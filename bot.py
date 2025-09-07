import json
import os
from datetime import datetime
from discord.utils import get

import discord
import requests
from discord.ext import tasks
import tokens

# --- Constants ---
OENGUS_API_URL = "https://oengus.io/api/v1/marathons"
MARATHONS_JSON_FILE = "current_marathons.json"
DISCORD_ALERTS_CHANNEL = "marathon-alerts"

client = discord.Client(intents=discord.Intents.default())


async def purge_channels():
    """
    Deletes all messages sent by the bot in every channel named DISCORD_ALERTS_CHANNEL.
    Also clears the tracked marathons JSON file.
    This is a destructive operation and should be used with caution.
    """
    print("Starting channel purge...")
    def is_me(m):
        return m.author == client.user

    # Clear the state file first
    save_tracked_marathons({})

    purged_channels = 0
    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.name == DISCORD_ALERTS_CHANNEL:
                try:
                    print(f"Purging messages from '{channel.name}' in guild '{guild.name}'...")
                    await channel.purge(check=is_me)
                    purged_channels += 1
                except discord.Forbidden:
                    print(f"Error: No permissions to purge messages in '{channel.name}' in guild '{guild.name}'.")
                except Exception as e:
                    print(f"An unexpected error occurred while purging channel {channel.id}: {e}")

    if purged_channels > 0:
        print(f"Successfully purged messages from {purged_channels} channel(s).")
    else:
        print("No channels found to purge, or all found channels had errors.")

def format_time(ts):
    """Formats a timestamp string from Oengus API to a more readable date format."""
    if not ts:
        return "N/A"
    try:
        return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').strftime("%m/%d/%Y")
    except (TypeError, ValueError):
        return ts


@client.event
async def on_ready():
    print(f'We have logged in as {client.user}')
    # Purging channels on startup is too aggressive.
    # The bot will already remove messages for marathons that are no longer open.
    # await purge_channels()
    sub_messages.start()


def load_tracked_marathons():
    """Loads the dictionary of tracked marathons from the JSON file."""
    if not os.path.exists(MARATHONS_JSON_FILE):
        return {}
    try:
        with open(MARATHONS_JSON_FILE) as marathon_file:
            return json.load(marathon_file)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error loading {MARATHONS_JSON_FILE}: {e}")
        return {} # Return empty dict on error to avoid crash

def save_tracked_marathons(marathon_dict):
    """Saves the dictionary of tracked marathons to the JSON file."""
    try:
        with open(MARATHONS_JSON_FILE, 'w') as newfile:
            json.dump(marathon_dict, newfile, indent=4)
    except IOError as e:
        print(f"Error saving {MARATHONS_JSON_FILE}: {e}")

def fetch_marathon_details(marathon_id):
    """Fetches detailed information for a single marathon."""
    try:
        details_response = requests.get(f"{OENGUS_API_URL}/{marathon_id}")
        details_response.raise_for_status()
        return details_response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching details for marathon {marathon_id}: {e}")
        return None

def create_marathon_embed(marathon, details):
    """Creates a Discord embed for a given marathon."""
    if marathon.get('onsite'):
        location = marathon.get('location')
        country = marathon.get('country')
        event_location = f"{location}, {country}" if location and country else "On-site"
    else:
        event_location = "Online"

    description = details.get('description')
    if not description:
        description = "No description provided."

    if len(description) > 500:
        description = description[:500] + "..."

    embed = discord.Embed(
        title=marathon['name'],
        url=f"https://oengus.io/marathon/{marathon['id']}",
        description=description,
        color=discord.Colour.random()
    )

    embed.add_field(name='Start Date', value=format_time(marathon.get('startDate')))
    embed.add_field(name="End Date", value=format_time(marathon.get('endDate')))
    embed.add_field(name="Submissions Open Until", value=format_time(marathon.get('submissionsEndDate')), inline=False)
    embed.add_field(name="Location", value=event_location)
    embed.add_field(name="Language", value=marathon.get('language', 'N/A').upper())
    embed.add_field(name="Max Runners", value=details.get('maxNumberOfScreens', 'N/A'))
    embed.add_field(name="Emulators Okay?", value="Yes" if details.get('emulatorAuthorized') else "No")

    if details.get('discordRequired'):
        discord_invite = f"Yes\nhttps://discord.gg/{details.get('discord', '')}"
        embed.add_field(name="Required to Join Discord?", value=discord_invite)
    else:
        embed.add_field(name="Required to Join Discord?", value="No")

    return embed


def find_alert_channel(client, channel_name):
    """Finds the first channel with a given name across all guilds."""
    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.name == channel_name:
                return channel
    return None

@tasks.loop(hours=1)
async def sub_messages():
    """The main task that runs every hour to check for marathon submission updates."""
    tracked_marathons = load_tracked_marathons()

    sub_channel = find_alert_channel(client, DISCORD_ALERTS_CHANNEL)
    if not sub_channel:
        print(f"Error: Could not find channel '{DISCORD_ALERTS_CHANNEL}'. Bot might not be in any guilds or channel does not exist.")
        return

    try:
        response = requests.get(OENGUS_API_URL)
        response.raise_for_status()
        api_data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data from Oengus API: {e}")
        return

    open_marathons = {marathon['id']: marathon for marathon in api_data.get('open', [])}
    open_marathon_ids = set(open_marathons.keys())
    tracked_marathon_ids = set(tracked_marathons.keys())

    # --- Remove closed marathons ---
    marathon_ids_to_remove = tracked_marathon_ids - open_marathon_ids
    for marathon_id in marathon_ids_to_remove:
        try:
            message_id = tracked_marathons[marathon_id]['msg_id']
            message = await sub_channel.fetch_message(message_id)
            await message.delete()
            print(f"Removed message for closed marathon: {marathon_id}")
        except discord.NotFound:
            print(f"Message for marathon {marathon_id} not found, assuming already deleted.")
        except discord.Forbidden:
            print(f"No permissions to delete message for marathon {marathon_id}.")
        finally:
            del tracked_marathons[marathon_id]

    # --- Add new marathons ---
    marathon_ids_to_add = open_marathon_ids - tracked_marathon_ids
    for marathon_id in marathon_ids_to_add:
        marathon = open_marathons[marathon_id]
        details = fetch_marathon_details(marathon_id)
        if details:
            try:
                embed = create_marathon_embed(marathon, details)
                message = await sub_channel.send(embed=embed)
                tracked_marathons[marathon_id] = {"msg_id": message.id}
                print(f"Posted new message for marathon: {marathon['name']}")
            except discord.Forbidden:
                print(f"No permissions to send messages in {DISCORD_ALERTS_CHANNEL}.")
            except Exception as e:
                print(f"An unexpected error occurred while processing marathon {marathon_id}: {e}")

    save_tracked_marathons(tracked_marathons)


client.run(tokens.DISCORD_TOKEN)
