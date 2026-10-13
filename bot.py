
import os
import discord
import aiohttp

TOKEN = os.environ.get("DISCORD_TOKEN")
ALERT_CHANNEL_ID = os.environ.get("ALERT_CHANNEL_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
DASHBOARD_INGEST_KEY = os.environ.get("DASHBOARD_INGEST_KEY", "")

KEYWORDS = [
    word.strip().lower()
    for word in os.environ.get(
        "WATCH_KEYWORDS",
        "social watch, project update, event announcement, urgent, launch, settings, owner info"
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

    if DASHBOARD_URL and DASHBOARD_INGEST_KEY:
        print("Dashboard forwarding is configured.")
    else:
        print("Dashboard forwarding is not configured yet.")


async def forward_to_dashboard(keyword, message):
    if not DASHBOARD_URL or not DASHBOARD_INGEST_KEY:
        return

    payload = {
        "keyword": keyword,
        "message": message.content[:1500],
        "server": message.guild.name,
        "channel": str(message.channel)
    }

    headers = {
        "Authorization": f"Bearer {DASHBOARD_INGEST_KEY}"
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{DASHBOARD_URL}/api/alerts",
                json=payload,
                headers=headers
            ) as response:
                if response.status == 201:
                    print(f"Dashboard alert sent: {keyword}")
                else:
                    print(
                        f"Dashboard returned HTTP {response.status}: "
                        f"{await response.text()}"
                    )

    except (aiohttp.ClientError, TimeoutError) as error:
        print(f"Could not forward alert to dashboard: {error}")


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
        await alert_channel.send(
            alert,
            allowed_mentions=discord.AllowedMentions.none()
        )
        print("Alert sent to Discord.")
    except discord.HTTPException as error:
        print(f"Could not send alert: {error}")

    for keyword in matches:
        await forward_to_dashboard(keyword, message)


client.run(TOKEN)
