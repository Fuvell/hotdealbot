import os
import json
import discord
from dotenv import load_dotenv
from discord.ext import tasks, commands
from datetime import datetime

# Import fetching functions for deals
from scrape_deals import fetch_hot_deals
from arca_crawler import fetch_hot_deals_arca

############################
# Load environment variables
############################
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN") or "YOUR_DISCORD_BOT_TOKEN_HERE"

############################
# Channel / posted IDs
############################
REGISTERED_CHANNELS_FILE = "registered_channels.json"
SENT_IDS_FILE = "sent_ids.txt"

# Load registered channels
if os.path.exists(REGISTERED_CHANNELS_FILE):
    with open(REGISTERED_CHANNELS_FILE, "r") as file:
        registered_channels = json.load(file)
else:
    registered_channels = {}

# Load sent deal IDs
if os.path.exists(SENT_IDS_FILE):
    with open(SENT_IDS_FILE, "r") as file:
        posted_deal_ids = set(file.read().splitlines())
else:
    posted_deal_ids = set()

############################
# Discord Setup
############################
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True  # Ensure you have enabled this in the Discord developer portal.
bot = commands.Bot(command_prefix="!", intents=intents)

############################
# Helper Function
############################
async def send_deal_embed(channel, deal):
    """
    Sends an embed message to the specified channel for a given deal.
    """
    embed = discord.Embed(
        title=deal.get("title", "Hot 핫딜"),
        url=deal.get("url", ""),
        color=int(deal.get('site_color', 'ff0000'), 16)
    )
    embed.set_thumbnail(url=deal.get("image_url", "https://via.placeholder.com/150"))
    embed.set_author(name=f"{deal.get('site_name', 'N/A')}", icon_url=f"{deal.get('logo', 'https://via.placeholder.com/150')}")
    embed.add_field(name=f"> **{deal.get('price', 'N/A')}**", value="", inline=True)
    embed.add_field(name=f"`[ {deal.get('category', '카테고리 없음')} ]`", value="", inline=True)
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    embed.set_footer(text=f"등록일: {current_time}")
    await channel.send(embed=embed)

############################
# Bot Events
############################
@bot.event
async def on_ready():
    print(f"{bot.user} (ID: {bot.user.id}) is online, searching for deals.")
    print(r"""
 ___  ___  ________  _________  ________  _______   ________  ___          
|\  \|\  \|\   __  \|\___   ___\\   ___ \|\  ___ \ |\   __  \|\  \         
\ \  \\\  \ \  \|\  \|___ \  \_\ \  \_|\ \ \   __/|\ \  \|\  \ \  \        
 \ \   __  \ \  \\\  \   \ \  \ \ \  \ \\ \ \  \_|/_\ \   __  \ \  \       
  \ \  \ \  \ \  \\\  \   \ \  \ \ \  \_\\ \ \  \_|\ \ \  \ \  \ \  \____  
   \ \__\ \__\ \_______\   \ \__\ \ \_______\ \_______\ \__\ \__\ \_______\ 
    \|__|\|__|\|_______|    \|__|  \|_______|\|_______|\|__|\|__|\|_______|
""")
    check_hot_deals.start()  # Start the background loop

############################
# Background Task
############################
@tasks.loop(minutes=1)
async def check_hot_deals():
    """
    Periodically fetches deals (in a thread) and posts new ones to the registered channels.
    """
    try:
        # Reload registered channels from the file in case new ones were added
        if os.path.exists(REGISTERED_CHANNELS_FILE):
            with open(REGISTERED_CHANNELS_FILE, "r") as file:
                global registered_channels
                registered_channels = json.load(file)

        # Fetch deals from Quasarzone and ArcaLive
        quasar_deals = await bot.loop.run_in_executor(None, fetch_hot_deals)
        arca_deals = await bot.loop.run_in_executor(None, fetch_hot_deals_arca)

        # Combine deals from both sources
        all_deals = quasar_deals + arca_deals

        # Process each deal for all channels
        for deal in all_deals:
            if deal["id"] in posted_deal_ids:
                continue  # Skip already sent deals

            # Try sending the deal to all channels
            for guild_id, channel_id in registered_channels.items():
                channel = bot.get_channel(channel_id)
                if channel is None:
                    print(f"Could not find the channel for guild ID {guild_id}. Skipping...")
                    continue

                try:
                    await send_deal_embed(channel, deal)
                except Exception as e:
                    print(f"Error sending deal embed to guild {guild_id}, channel {channel_id}: {e}")

            # Mark the deal as sent after attempting all channels
            posted_deal_ids.add(deal["id"])
            with open(SENT_IDS_FILE, "a") as file:
                file.write(f"{deal['id']}\n")

    except Exception as e:
        print(f"Error while fetching or sending deals: {e}")

############################
# Commands
############################
@bot.command(name="setchannel")
async def set_channel(ctx):
    guild_id = str(ctx.guild.id)
    channel_id = ctx.channel.id

    # Update the registered channels
    registered_channels[guild_id] = channel_id
    with open(REGISTERED_CHANNELS_FILE, "w") as file:
        json.dump(registered_channels, file, indent=4)

    await ctx.send(f"채널 등록 완료!")
    print(f"Channel {channel_id} has been registered for guild {guild_id}.")
    print(f"Updated registered channels: {registered_channels}")

    # Fetch and send current deals to the new channel immediately
    try:
        quasar_deals = await bot.loop.run_in_executor(None, fetch_hot_deals)
        arca_deals = await bot.loop.run_in_executor(None, fetch_hot_deals_arca)
        all_deals = quasar_deals + arca_deals

        channel = bot.get_channel(channel_id)
        if channel is None:
            print(f"Error: Could not find the channel {channel_id}")
            return

        for deal in all_deals:
            if deal["id"] not in posted_deal_ids:
                posted_deal_ids.add(deal["id"])
                with open(SENT_IDS_FILE, "a") as file:
                    file.write(f"{deal['id']}\n")
                await send_deal_embed(channel, deal)

    except Exception as e:
        print(f"Error sending immediate deals to channel {channel_id}: {e}")

############################
# Run the Bot
############################
bot.run(TOKEN)
