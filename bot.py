import asyncio
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord


TOKEN = os.environ.get("DISCORD_TOKEN")
ALERT_CHANNEL_ID = os.environ.get("ALERT_CHANNEL_ID", "").strip()
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")
DASHBOARD_INGEST_KEY = os.environ.get("DASHBOARD_INGEST_KEY", "").strip()
HOME_ASSISTANT_WEBHOOK_URL = os.environ.get(
    "HOME_ASSISTANT_WEBHOOK_URL", ""
).strip()

DEFAULT_KEYWORDS = [
    word.strip().lower()
    for word in os.environ.get(
        "WATCH_KEYWORDS",
        "social watch, project update, event announcement, urgent, launch, settings, owner info",
    ).split(",")
    if word.strip()
]

HIGH_PRIORITY_TERMS = {"urgent", "emergency", "critical", "immediate"}
MEDIUM_PRIORITY_TERMS = {"announcement", "important", "warning"}


def fallback_priority(term):
    if term in HIGH_PRIORITY_TERMS:
        return "high"
    if term in MEDIUM_PRIORITY_TERMS:
        return "medium"
    return "normal"


KEYWORD_RULES = [
    {"term": term, "priority": fallback_priority(term)}
    for term in DEFAULT_KEYWORDS
]
AUTOMATION_SETTINGS = {
    "discordChannelAlertsEnabled": True,
    "homeAssistantAnnouncementsEnabled": True,
    "nestAnnouncementMode": "all",
    "quietHoursEnabled": False,
    "quietHoursStart": "22:00",
    "quietHoursEnd": "07:00",
    "timezone": "America/New_York",
}
_CONFIG_SYNC_TASK = None

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")

if not ALERT_CHANNEL_ID.isdigit():
    raise RuntimeError("ALERT_CHANNEL_ID is missing or invalid")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def post_integration_heartbeat(service, status="connected"):
    if not DASHBOARD_URL or not DASHBOARD_INGEST_KEY:
        return

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{DASHBOARD_URL}/api/integrations/heartbeat",
                headers={"Authorization": f"Bearer {DASHBOARD_INGEST_KEY}"},
                json={"service": service, "status": status},
            ) as response:
                if response.status >= 300:
                    print(
                        f"Integration heartbeat failed for {service}: "
                        f"HTTP {response.status}"
                    )
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(f"Integration heartbeat unavailable: {type(error).__name__}")


async def refresh_dashboard_config():
    """Fetch dashboard-managed keywords and notification settings."""
    global KEYWORD_RULES, AUTOMATION_SETTINGS

    if not DASHBOARD_URL or not DASHBOARD_INGEST_KEY:
        return False

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{DASHBOARD_URL}/api/bot-config",
                headers={"Authorization": f"Bearer {DASHBOARD_INGEST_KEY}"},
            ) as response:
                if response.status != 200:
                    print(f"Dashboard config sync failed: HTTP {response.status}")
                    return False
                data = await response.json()

        remote_rules = data.get("keywords")
        remote_automations = data.get("automations")

        if isinstance(remote_rules, list):
            clean_rules = []
            seen = set()
            for rule in remote_rules[:100]:
                if not isinstance(rule, dict):
                    continue
                term = str(rule.get("term", "")).strip().lower()[:80]
                if not term or term in seen:
                    continue
                seen.add(term)
                priority = str(rule.get("priority", "normal")).lower()
                if priority not in {"normal", "medium", "high"}:
                    priority = "normal"
                clean_rules.append({"term": term, "priority": priority})
            KEYWORD_RULES = clean_rules

        if isinstance(remote_automations, dict):
            allowed_modes = {"all", "medium_high", "high_only", "off"}
            mode = str(remote_automations.get("nestAnnouncementMode", "all"))
            if mode not in allowed_modes:
                mode = "all"

            timezone = str(
                remote_automations.get("timezone", "America/New_York")
            )
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                timezone = "America/New_York"

            start = str(remote_automations.get("quietHoursStart", "22:00"))
            end = str(remote_automations.get("quietHoursEnd", "07:00"))
            if not valid_hhmm(start):
                start = "22:00"
            if not valid_hhmm(end):
                end = "07:00"

            AUTOMATION_SETTINGS = {
                "discordChannelAlertsEnabled": (
                    remote_automations.get("discordChannelAlertsEnabled") is not False
                ),
                "homeAssistantAnnouncementsEnabled": (
                    remote_automations.get("homeAssistantAnnouncementsEnabled") is not False
                ),
                "nestAnnouncementMode": mode,
                "quietHoursEnabled": bool(
                    remote_automations.get("quietHoursEnabled", False)
                ),
                "quietHoursStart": start,
                "quietHoursEnd": end,
                "timezone": timezone,
            }

        print(
            f"Dashboard settings synced: {len(KEYWORD_RULES)} active keywords; "
            f"Nest mode={AUTOMATION_SETTINGS['nestAnnouncementMode']}."
        )
        return True

    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        print(f"Dashboard settings unavailable: {type(error).__name__}")
        return False


def valid_hhmm(value):
    try:
        time.fromisoformat(value)
        return len(value) == 5 and value[2] == ":"
    except (TypeError, ValueError):
        return False


async def config_sync_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await refresh_dashboard_config()
        await post_integration_heartbeat("discord_bot", "connected")
        await asyncio.sleep(60)


@client.event
async def on_ready():
    global _CONFIG_SYNC_TASK

    print(f"Social Watch is online as {client.user}")

    if DASHBOARD_URL and DASHBOARD_INGEST_KEY:
        print("Dashboard forwarding is configured.")
        await refresh_dashboard_config()
        await post_integration_heartbeat("discord_bot", "connected")
    else:
        print("Dashboard forwarding is not configured yet.")

    if HOME_ASSISTANT_WEBHOOK_URL.startswith("https://"):
        print("Home Assistant webhook forwarding is configured.")
    elif HOME_ASSISTANT_WEBHOOK_URL:
        print("Home Assistant webhook is set but must use HTTPS; forwarding is disabled.")
    else:
        print("Home Assistant webhook is not configured yet.")

    if _CONFIG_SYNC_TASK is None or _CONFIG_SYNC_TASK.done():
        _CONFIG_SYNC_TASK = asyncio.create_task(config_sync_loop())


def get_priority(matches):
    ranking = {"normal": 1, "medium": 2, "high": 3}
    priorities = [
        rule.get("priority", "normal")
        for rule in matches
        if rule.get("priority") in ranking
    ]
    if not priorities:
        return "normal"
    return max(priorities, key=lambda value: ranking[value])


def matches_quiet_hours(settings):
    if not settings.get("quietHoursEnabled"):
        return False

    try:
        zone = ZoneInfo(settings.get("timezone", "America/New_York"))
        now = datetime.now(zone).time().replace(tzinfo=None)
        start = time.fromisoformat(settings.get("quietHoursStart", "22:00"))
        end = time.fromisoformat(settings.get("quietHoursEnd", "07:00"))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False

    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def should_announce(priority):
    settings = AUTOMATION_SETTINGS
    if not settings.get("homeAssistantAnnouncementsEnabled", True):
        return False

    mode = settings.get("nestAnnouncementMode", "all")
    if mode == "off":
        return False
    if mode == "high_only" and priority != "high":
        return False
    if mode == "medium_high" and priority not in {"medium", "high"}:
        return False
    if matches_quiet_hours(settings):
        return False
    return True


async def forward_to_dashboard(keyword_matches, discord_message, priority):
    """Send one complete alert for a matched message to the dashboard."""
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
    matched_keywords = ", ".join(rule["term"] for rule in keyword_matches)
    dashboard_message = (
        f"Server: {guild_name}\n"
        f"Channel: #{channel_name}\n"
        f"Author: {discord_message.author}\n"
        f"Matched keywords: {matched_keywords}\n"
        f"Message: {discord_message.content[:8500]}\n"
        f"Discord link: {discord_message.jump_url}"
    )

    payload = {
        "message": dashboard_message[:10000],
        "source": f"Discord: {guild_name} / #{channel_name}"[:150],
        "priority": priority,
        "keyword": matched_keywords[:150],
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{DASHBOARD_URL}/api/alerts",
                json=payload,
                headers={
                    "Authorization": f"Bearer {DASHBOARD_INGEST_KEY}",
                    "Content-Type": "application/json",
                },
            ) as response:
                if response.status == 201:
                    print(
                        f"Dashboard alert saved: priority={priority}, "
                        f"keywords={matched_keywords}"
                    )
                    return True
                detail = (await response.text())[:250]
                print(f"Dashboard alert failed: HTTP {response.status}: {detail}")
                return False
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(f"Could not forward alert to dashboard: {type(error).__name__}")
        return False


async def forward_to_home_assistant(keyword_matches, discord_message, priority):
    """Forward only alerts allowed by dashboard announcement preferences."""
    if not HOME_ASSISTANT_WEBHOOK_URL:
        return False
    if not HOME_ASSISTANT_WEBHOOK_URL.startswith("https://"):
        print("Home Assistant webhook skipped: URL must use HTTPS.")
        return False
    if not should_announce(priority):
        return False

    guild_name = discord_message.guild.name
    channel_name = getattr(
        discord_message.channel, "name", str(discord_message.channel)
    )
    matched_keywords = ", ".join(rule["term"] for rule in keyword_matches)
    spoken_message = (
        f"Social Watch {priority} priority alert. Server {guild_name}, "
        f"channel {channel_name}. Matched keywords: {matched_keywords}. "
        f"Message: {discord_message.content[:400]}"
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
                    print(f"Home Assistant accepted Nest alert: priority={priority}.")
                    await post_integration_heartbeat("home_assistant", "connected")
                    return True

                print(f"Home Assistant webhook failed: HTTP {response.status}.")
                await post_integration_heartbeat("home_assistant", "error")
                return False
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(f"Home Assistant webhook unavailable: {type(error).__name__}")
        await post_integration_heartbeat("home_assistant", "error")
        return False


@client.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    content = message.content.lower()
    matches = [
        rule for rule in KEYWORD_RULES
        if rule["term"] in content
    ]
    if not matches:
        return

    matched_terms = [rule["term"] for rule in matches]
    priority = get_priority(matches)

    print(
        f"Keyword alert | Server: {message.guild.name} "
        f"| Channel: {message.channel} "
        f"| Priority: {priority} "
        f"| Matches: {', '.join(matched_terms)}"
    )

    # Discord channel delivery is controlled from the dashboard.
    if AUTOMATION_SETTINGS.get("discordChannelAlertsEnabled", True):
        alert_channel = client.get_channel(int(ALERT_CHANNEL_ID))
        if alert_channel is None:
            try:
                alert_channel = await client.fetch_channel(int(ALERT_CHANNEL_ID))
            except discord.HTTPException as error:
                print(f"Could not find Discord alert channel: {error}")
                alert_channel = None

        if alert_channel is not None:
            channel_name = getattr(message.channel, "name", str(message.channel))
            alert = (
                "**Social Watch Alert**\n"
                f"**Priority:** {priority.upper()}\n"
                f"**Server:** {message.guild.name}\n"
                f"**Channel:** #{channel_name}\n"
                f"**Author:** {message.author}\n"
                f"**Matched keywords:** {', '.join(matched_terms)}\n"
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
    else:
        print("Discord channel notifications are disabled in dashboard settings.")

    # Store the alert regardless of whether any announcement is suppressed.
    await forward_to_dashboard(matches, message, priority)
    await forward_to_home_assistant(matches, message, priority)


client.run(TOKEN)
