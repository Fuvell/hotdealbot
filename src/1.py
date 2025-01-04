import discord
from discord.ext import commands

TOKEN = "MTMxODA5NjA0OTEyMjA1MDEyOQ.Gyo4D3.jUw5KWNu9heLR9rO6t7z8hvoHfHlqtlPIUb224"

intents = discord.Intents.default()
intents.messages = True  # Ensure message handling is enabled
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}!")

@bot.command(name="test")
async def test_command(ctx):
    await ctx.send("Test command works!")

bot.run(TOKEN)
