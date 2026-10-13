import asyncio
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord import app_commands


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

tree = app_commands.CommandTree(client)
management_group = app_commands.Group(
    name="socialwatch",
    description="Secure Social Watch server-management commands",
)
ADMIN_USER_IDS = {
    item.strip()
    for item in os.environ.get("SOCIAL_WATCH_ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}
MANAGEMENT_GUILD_ID = os.environ.get("MANAGEMENT_GUILD_ID", "").strip()


async def is_authorized_admin(interaction: discord.Interaction) -> bool:
    """Require an explicitly allowlisted user as well as Manage Server."""
    if interaction.guild is None or interaction.user is None:
        return False
    return str(interaction.user.id) in ADMIN_USER_IDS


def bot_has_guild_permission(guild: discord.Guild, permission: str) -> bool:
    member = guild.me
    return bool(member and getattr(member.guild_permissions, permission, False))


async def management_denied(interaction: discord.Interaction, message: str):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@management_group.command(name="channels", description="List this server's channels and categories")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_channels(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await management_denied(interaction, "Use this command inside a server.")
    lines = []
    for channel in sorted(guild.channels, key=lambda item: (item.position, item.name.lower())):
        kind = "category" if isinstance(channel, discord.CategoryChannel) else (
            "voice" if isinstance(channel, discord.VoiceChannel) else
            "text" if isinstance(channel, discord.TextChannel) else "other"
        )
        lines.append(f"• {channel.name} — {kind} — ID: {channel.id}")
    body = "\\n".join(lines) or "This server has no visible channels."
    await interaction.response.send_message(body[:1900], ephemeral=True)


@management_group.command(name="create-channel", description="Create a text or voice channel")
@app_commands.describe(name="New channel name", channel_type="Text or voice channel", category="Optional existing category name")
@app_commands.choices(channel_type=[
    app_commands.Choice(name="Text", value="text"),
    app_commands.Choice(name="Voice", value="voice"),
])
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True, manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_create_channel(
    interaction: discord.Interaction,
    name: str,
    channel_type: app_commands.Choice[str],
    category: str = "",
):
    guild = interaction.guild
    if guild is None:
        return await management_denied(interaction, "Use this command inside a server.")
    if not bot_has_guild_permission(guild, "manage_channels"):
        return await management_denied(interaction, "Social Watch needs the Manage Channels permission.")
    clean_name = name.strip().lower().replace(" ", "-")[:100]
    if not clean_name:
        return await management_denied(interaction, "Enter a valid channel name.")
    parent = None
    if category.strip():
        parent = discord.utils.find(
            lambda item: item.name.casefold() == category.strip().casefold(),
            guild.categories,
        )
        if parent is None:
            return await management_denied(interaction, f"I couldn't find a category named '{category}'.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if channel_type.value == "voice":
            created = await guild.create_voice_channel(clean_name, category=parent, reason=f"Social Watch command by {interaction.user.id}")
        else:
            created = await guild.create_text_channel(clean_name, category=parent, reason=f"Social Watch command by {interaction.user.id}")
        await interaction.followup.send(f"Created {created.mention} (ID: {created.id}).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Discord denied this action. Check Social Watch's channel permissions and role position.", ephemeral=True)
    except discord.HTTPException as error:
        await interaction.followup.send(f"Discord couldn't create the channel: {error}", ephemeral=True)


@management_group.command(name="create-category", description="Create a channel category")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True, manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_create_category(interaction: discord.Interaction, name: str):
    guild = interaction.guild
    if guild is None:
        return await management_denied(interaction, "Use this command inside a server.")
    if not bot_has_guild_permission(guild, "manage_channels"):
        return await management_denied(interaction, "Social Watch needs the Manage Channels permission.")
    clean_name = name.strip()[:100]
    if not clean_name:
        return await management_denied(interaction, "Enter a category name.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        created = await guild.create_category(clean_name, reason=f"Social Watch command by {interaction.user.id}")
        await interaction.followup.send(f"Created category **{created.name}** (ID: {created.id}).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Discord denied this action. Check Social Watch's Manage Channels permission.", ephemeral=True)
    except discord.HTTPException as error:
        await interaction.followup.send(f"Discord couldn't create the category: {error}", ephemeral=True)


@management_group.command(name="create-role", description="Create a role with an optional hex color")
@app_commands.describe(name="New role name", color="Hex color such as #5865F2")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_roles=True, manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_create_role(
    interaction: discord.Interaction,
    name: str,
    color: str = "#5865F2",
):
    guild = interaction.guild
    if guild is None:
        return await management_denied(interaction, "Use this command inside a server.")
    if not bot_has_guild_permission(guild, "manage_roles"):
        return await management_denied(interaction, "Social Watch needs the Manage Roles permission.")
    clean_name = name.strip()[:100]
    if not clean_name:
        return await management_denied(interaction, "Enter a role name.")
    try:
        role_color = discord.Color(int(color.strip().lstrip("#"), 16))
    except (ValueError, AttributeError):
        return await management_denied(interaction, "Use a valid hex color, for example #5865F2.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        created = await guild.create_role(name=clean_name, colour=role_color, reason=f"Social Watch command by {interaction.user.id}")
        await interaction.followup.send(f"Created role **{created.name}** (ID: {created.id}).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Discord denied this action. Check Manage Roles and ensure the bot's top role is high enough.", ephemeral=True)
    except discord.HTTPException as error:
        await interaction.followup.send(f"Discord couldn't create the role: {error}", ephemeral=True)


@management_group.command(name="rename-server", description="Change this server's name")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_rename_server(interaction: discord.Interaction, name: str):
    guild = interaction.guild
    if guild is None:
        return await management_denied(interaction, "Use this command inside a server.")
    if not bot_has_guild_permission(guild, "manage_guild"):
        return await management_denied(interaction, "Social Watch needs the Manage Server permission.")
    clean_name = name.strip()[:100]
    if not clean_name:
        return await management_denied(interaction, "Enter a server name.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await guild.edit(name=clean_name, reason=f"Social Watch command by {interaction.user.id}")
        await interaction.followup.send(f"Server renamed to **{clean_name}**.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Discord denied this action. Check Social Watch's Manage Server permission.", ephemeral=True)
    except discord.HTTPException as error:
        await interaction.followup.send(f"Discord couldn't rename the server: {error}", ephemeral=True)


tree.add_command(management_group)


@tree.error
async def on_management_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = "Access denied. You need Manage Server plus your Discord user ID must be listed in SOCIAL_WATCH_ADMIN_IDS in Railway."
    else:
        print(f"Social Watch command error: {type(error).__name__}: {error}")
        message = "The command failed. Check the Social Watch Railway logs for details."
    await management_denied(interaction, message)


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

    try:
        if MANAGEMENT_GUILD_ID.isdigit():
            target_guild = discord.Object(id=int(MANAGEMENT_GUILD_ID))
            tree.copy_global_to(guild=target_guild)
            synced = await tree.sync(guild=target_guild)
            print(f"Synced {len(synced)} management commands to guild {MANAGEMENT_GUILD_ID}.")
        else:
            synced = await tree.sync()
            print(f"Synced {len(synced)} global management commands.")
        if not ADMIN_USER_IDS:
            print("WARNING: SOCIAL_WATCH_ADMIN_IDS is empty; management commands are locked to everyone.")
    except discord.HTTPException as error:
        print(f"Could not sync management commands: {type(error).__name__}: {error}")

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
