
import os
import discord

TOKEN = os.environ.get("MTU1OTU3Mjc3NjU4NTUyNzQyNw.GJSu5E.5yYJTec17_UkiBy0NFOe_ELzxuO3NI5oS4bcZc")

KEYWORDS = [
    word.strip().lower()
    for word in os.environ.get("WATCH_KEYWORDS", "social watch").split(",")
    if word.strip()
]

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Social Watch is online as {client.user}")


@client.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.lower()
    matches = [word for word in KEYWORDS if word in content]

    if matches:
        print(
            f"Keyword alert | Server: "
            f"{message.guild.name if message.guild else 'DM'}"
            f" | Channel: {message.channel}"
            f" | Author: {message.author}"
            f" | Matches: {', '.join(matches)}"
        )


if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is missing"
    )

client.run(TOKEN)
