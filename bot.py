
import os
import discord

TOKEN = os.environ.get("DISCORD_TOKEN")
ALERT_CHANNEL_ID = os.environ.get("ALERT_CHANNEL_ID")

KEYWORDS = [
    word.strip().lower()
    for word in os.environ.get(
        "WATCH_KEYWORDS", "social watch"
    ).split(",")
    if word.strip()
]

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")

if not ALERT_CHANNEL_ID or not ALERT_CHANNEL_ID.isdigit():
    raise RuntimeError("ALERT_CHANNEL_ID is missing or invalid")

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Social Watch is online as {client.user}")


@client.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    content = message.content.lower()
    matches = [word for word in KEYWORDS if word in content]

    if not matches:
        return

    print(
        f"Keyword alert | Server: {message.guild.name} "
        f"| Channel: {message.channel} "
        f"| Matches: {', '.join(matches)}"
    )
    print(f"DEBUG: Looking up alert channel ID: {ALERT_CHANNEL_ID}")
    print(f"DEBUG: Looking up alert channel ID: {ALERT_CHANNEL_ID}")
    alert_channel = client.get_channel(int(ALERT_CHANNEL_ID))

    if alert_channel is None:
        try:
            alert_channel = await client.fetch_channel(
                int(ALERT_CHANNEL_ID)
            )
        except discord.HTTPException as error:
            print(f"Could not find alert channel: {error}")
            return

    alert = (
        f"**Social Watch Alert**\n"
        f"**Server:** {message.guild.name}\n"
        f"**Channel:** #{message.channel.name}\n"
        f"**Author:** {message.author}\n"
        f"**Matched keywords:** {', '.join(matches)}\n"
        f"**Message:** {message.content[:1200]}"
    )

    try:
        print("DEBUG: Attempting to send alert")
        await alert_channel.send(
            alert,
            allowed_mentions=discord.AllowedMentions.none()
        )
        print("Alert sent to Discord.")
    except discord.HTTPException as error:
        print(f"Could not send alert: {error}")


client.run(TOKEN)
