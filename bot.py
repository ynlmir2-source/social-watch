import asyncio
import os

import aiohttp
import discord


TOKEN = os.environ.get("DISCORD_TOKEN")
ALERT_CHANNEL_ID = os.environ.get("ALERT_CHANNEL_ID", "").strip()
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")
DASHBOARD_INGEST_KEY = os.environ.get("DASHBOARD_INGEST_KEY", "").strip()
HOME_ASSISTANT_WEBHOOK_URL = os.environ.get(
    "HOME_ASSISTANT_WEBHOOK_URL", ""
).strip()

KEYWORDS = [
    word.strip().lower()
    for word in os.environ.get(
        "WATCH_KEYWORDS",
        "social watch, project update, event announcement, urgent, launch, settings, owner info",
    ).split(",")
    if word.strip()
]

HIGH_PRIORITY_KEYWORDS = {"urgent", "emergency", "critical", "immediate"}
MEDIUM_PRIORITY_KEYWORDS = {"announcement", "important", "warning"}

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")

if not ALERT_CHANNEL_ID.isdigit():
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

    if HOME_ASSISTANT_WEBHOOK_URL:
        if HOME_ASSISTANT_WEBHOOK_URL.startswith("https://"):
            print("Home Assistant webhook forwarding is configured.")
        else:
            print(
                "Home Assistant webhook is set but must use HTTPS. "
                "Forwarding to Home Assistant is disabled."
            )
    else:
        print("Home Assistant webhook is not configured yet.")


def get_priority(matches):
    matched = set(matches)

    if matched & HIGH_PRIORITY_KEYWORDS:
        return "high"

    if matched & MEDIUM_PRIORITY_KEYWORDS:
        return "medium"

    return "normal"


async def forward_to_dashboard(keyword_matches, discord_message):
    """Send one complete alert per matching Discord message to the dashboard."""
    if not DASHBOARD_URL or not DASHBOARD_INGEST_KEY:
        print("Dashboard alert skipped: forwarding variables are missing.")
        return False

    if not DASHBOARD_URL.startswith("https://"):
        print("Dashboard alert skipped: DASHBOARD_URL must use HTTPS.")
        return False

    guild_name = discord_message.guild.name
    channel_name = getattr(
        discord_message.channel, "name", str(discord_message.channel)
    )
    author_name = str(discord_message.author)
    matched_keywords = ", ".join(keyword_matches)

    dashboard_message = (
        f"Server: {guild_name}\n"
        f"Channel: #{channel_name}\n"
        f"Author: {author_name}\n"
        f"Matched keywords: {matched_keywords}\n"
        f"Message: {discord_message.content[:8500]}\n"
        f"Discord link: {discord_message.jump_url}"
    )

    payload = {
        "message": dashboard_message[:10000],
        "source": f"Discord: {guild_name} / #{channel_name}"[:150],
        "priority": get_priority(keyword_matches),
        "keyword": matched_keywords[:150],
    }

    headers = {
        "Authorization": f"Bearer {DASHBOARD_INGEST_KEY}",
        "Content-Type": "application/json",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{DASHBOARD_URL}/api/alerts",
                json=payload,
                headers=headers,
            ) as response:
                if response.status == 201:
                    print(
                        "Dashboard alert saved. "
                        f"Source: {payload['source']} | "
                        f"Keywords: {matched_keywords}"
                    )
                    return True

                response_detail = (await response.text())[:300]
                print(
                    f"Dashboard alert failed with HTTP {response.status}: "
                    f"{response_detail}"
                )
                return False

    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(f"Could not forward alert to dashboard: {type(error).__name__}")
        return False


async def forward_to_home_assistant(keyword_matches, discord_message):
    """POST a concise alert to Home Assistant's private webhook URL."""
    if not HOME_ASSISTANT_WEBHOOK_URL:
        return False

    if not HOME_ASSISTANT_WEBHOOK_URL.startswith("https://"):
        print("Home Assistant webhook skipped: URL must use HTTPS.")
        return False

    guild_name = discord_message.guild.name
    channel_name = getattr(
        discord_message.channel, "name", str(discord_message.channel)
    )
    matched_keywords = ", ".join(keyword_matches)
    priority = get_priority(keyword_matches)

    spoken_message = (
        f"Social Watch alert. Server {guild_name}, channel {channel_name}. "
        f"Matched keywords: {matched_keywords}. "
        f"Message: {discord_message.content[:500]}"
    )

    payload = {
        "message": spoken_message[:900],
        "source": f"Discord: {guild_name} / #{channel_name}"[:150],
        "priority": priority,
        "keyword": matched_keywords[:150],
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                HOME_ASSISTANT_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                if 200 <= response.status < 300:
                    print(
                        "Home Assistant webhook accepted alert. "
                        f"Priority: {priority} | Keywords: {matched_keywords}"
                    )
                    return True

                print(
                    f"Home Assistant webhook failed with HTTP {response.status}."
                )
                return False

    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(
            "Could not forward alert to Home Assistant: "
            f"{type(error).__name__}"
        )
        return False


@client.event
async def on_message(message):
    # Only monitor messages in servers, and never react to bot messages.
    if message.author.bot or message.guild is None:
        return

    content = message.content.lower()
    matches = list(dict.fromkeys(
        keyword for keyword in KEYWORDS if keyword in content
    ))

    if not matches:
        return

    print(
        f"Keyword alert | Server: {message.guild.name} "
        f"| Channel: {message.channel} "
        f"| Matches: {', '.join(matches)}"
    )

    # Send the alert to the configured Discord alert channel.
    alert_channel = client.get_channel(int(ALERT_CHANNEL_ID))

    if alert_channel is None:
        try:
            alert_channel = await client.fetch_channel(int(ALERT_CHANNEL_ID))
        except discord.HTTPException as error:
            # Keep going: the dashboard should still receive the alert.
            print(f"Could not find Discord alert channel: {error}")

    if alert_channel is not None:
        channel_name = getattr(message.channel, "name", str(message.channel))
        alert = (
            "**Social Watch Alert**\n"
            f"**Server:** {message.guild.name}\n"
            f"**Channel:** #{channel_name}\n"
            f"**Author:** {message.author}\n"
            f"**Matched keywords:** {', '.join(matches)}\n"
            f"**Message:** {message.content[:1200]}"
        )

        try:
            await alert_channel.send(
                alert,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            print("Alert sent to Discord.")
        except discord.DiscordException as error:
            print(f"Could not send alert to Discord: {error}")

    # Keep the dashboard integration and add the optional Nest Hub route.
    await forward_to_dashboard(matches, message)
    await forward_to_home_assistant(matches, message)


client.run(TOKEN)
