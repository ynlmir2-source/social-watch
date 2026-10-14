import asyncio
import builtins
import logging
import os
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord import app_commands
from infra_manager import InfrastructureManager, InfrastructureError


TOKEN = os.environ.get("DISCORD_TOKEN")
ALERT_CHANNEL_ID = os.environ.get("ALERT_CHANNEL_ID", "").strip()
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")
DASHBOARD_INGEST_KEY = os.environ.get("DASHBOARD_INGEST_KEY", "").strip()
HOME_ASSISTANT_WEBHOOK_URL = os.environ.get(
    "HOME_ASSISTANT_WEBHOOK_URL", ""
).strip()
HOME_ASSISTANT_URL = os.environ.get("HOME_ASSISTANT_URL", "").strip().rstrip("/")
HOME_ASSISTANT_TOKEN = os.environ.get("HOME_ASSISTANT_TOKEN", "").strip()
NEST_MEDIA_PLAYER_ENTITY_ID = os.environ.get("NEST_MEDIA_PLAYER_ENTITY_ID", "").strip()

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

LOG_CHANNEL_NAME = "social-watch-logs"
STATUS_CHANNEL_SUFFIX = "・system-status"
_LOG_CHANNEL = None
_STATUS_CHANNELS = {}
_LAST_LOG_ERROR_AT = 0.0

def _classify_log(message, explicit_level=None):
    text = str(message).lower()
    if explicit_level:
        level = str(explicit_level).lower()
        return level if level in {"debug", "info", "warning", "error", "critical"} else "info"
    if any(word in text for word in ("critical", "traceback", "exception", "failed", "failure", "error", "could not", "unavailable", "denied")):
        return "error"
    if "warning" in text or "warn" in text:
        return "warning"
    if "debug" in text:
        return "debug"
    return "info"

def _safe_log_text(value):
    text = str(value).replace("\x00", "")[:9000]
    text = re.sub(r"(?:mfa\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,})", "[REDACTED_TOKEN]", text)
    text = re.sub(r"(Bearer\s+)[^\s'\"]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(https://[^\s/]+/api/webhooks/)[^\s]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    return text

async def _post_log_to_dashboard(level, message, source="social-watch-bot"):
    if not DASHBOARD_URL.startswith("https://") or not DASHBOARD_INGEST_KEY:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{DASHBOARD_URL}/api/logs",
                headers={"Authorization": f"Bearer {DASHBOARD_INGEST_KEY}"},
                json={"level": level, "source": source, "message": message},
            ) as response:
                if response.status >= 300:
                    builtins.print(f"Log forwarding failed: dashboard returned HTTP {response.status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        builtins.print(f"Log forwarding unavailable: {type(error).__name__}")

async def _publish_log(message, level="info", source="social-watch-bot"):
    safe_message = _safe_log_text(message)
    if not safe_message:
        return
    global _LAST_LOG_ERROR_AT
    if level in {"warning", "error", "critical"}:
        _LAST_LOG_ERROR_AT = asyncio.get_running_loop().time()
        asyncio.create_task(_update_status_channels())
    await _post_log_to_dashboard(level, safe_message, source)
    channel = _LOG_CHANNEL
    if channel is not None:
        label = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING", "error": "ERROR", "critical": "CRITICAL"}.get(level, "INFO")
        try:
            await channel.send(
                f"**{label}** · {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n{safe_message[:1850]}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.DiscordException, RuntimeError) as error:
            builtins.print(f"Discord log-channel delivery failed: {type(error).__name__}")

def print(*args, **kwargs):
    """Keep Railway stdout intact while mirroring explicit app logs to the log center."""
    builtins.print(*args, **kwargs)
    try:
        message = " ".join(str(arg) for arg in args)
        if not message or not client.is_ready():
            return
        level = _classify_log(message)
        asyncio.get_running_loop().create_task(_publish_log(message, level))
    except RuntimeError:
        pass

class _DiscordLogHandler(logging.Handler):
    def emit(self, record):
        try:
            # Never forward discord.py's own HTTP/rate-limit logs back to Discord.
            # Doing so can create a feedback loop when the log channel is throttled.
            if record.name == "discord.http" or record.name.startswith("discord.http."):
                return
            message = self.format(record)
            if not client.is_ready():
                return
            if "rate limited" in message.lower() or "responded with 429" in message.lower():
                return
            level = _classify_log(message, record.levelname)
            asyncio.get_running_loop().create_task(_publish_log(message, level, "python-discord"))
        except Exception:
            pass

logging.getLogger().setLevel(logging.INFO)
_log_handler = _DiscordLogHandler()
_log_handler.setLevel(logging.INFO)
_log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logging.getLogger().addHandler(_log_handler)

async def _ensure_control_channels():
    """Create Social Watch control channels only in its explicitly configured home guild."""
    global _LOG_CHANNEL
    if not MANAGEMENT_GUILD_ID.isdigit():
        builtins.print("Social Watch control-channel setup skipped: MANAGEMENT_GUILD_ID is not configured.")
        return
    guild = client.get_guild(int(MANAGEMENT_GUILD_ID))
    if guild is None:
        builtins.print("Social Watch control-channel setup skipped: configured management guild is not available to the bot.")
        return
    existing_log = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if existing_log is None and bot_has_guild_permission(guild, "manage_channels"):
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True)
        for user_id in ADMIN_USER_IDS:
            member = guild.get_member(int(user_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(user_id))
                except discord.DiscordException:
                    member = None
            if member:
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        try:
            existing_log = await guild.create_text_channel(
                LOG_CHANNEL_NAME, overwrites=overwrites,
                reason="Social Watch central log channel",
                topic="Private system logs from Social Watch bot and integrations."
            )
        except discord.DiscordException as error:
            builtins.print(f"Could not create the Social Watch log channel: {type(error).__name__}")
    _LOG_CHANNEL = existing_log
    status_channel = next((channel for channel in guild.voice_channels if channel.name.endswith(STATUS_CHANNEL_SUFFIX)), None)
    if status_channel is None and bot_has_guild_permission(guild, "manage_channels"):
        try:
            status_channel = await guild.create_voice_channel(
                "🟢" + STATUS_CHANNEL_SUFFIX,
                reason="Social Watch live service status",
                user_limit=0
            )
        except discord.DiscordException as error:
            builtins.print(f"Could not create status voice channel in {guild.name}: {type(error).__name__}")
    if status_channel is not None:
        _STATUS_CHANNELS[guild.id] = status_channel
    await _update_status_channels()
    builtins.print(
        "Social Watch system logging ready: "
        + ("Discord log channel ID " + str(_LOG_CHANNEL.id) if _LOG_CHANNEL else "Discord log channel unavailable")
        + "; status voice channels=" + str(len(_STATUS_CHANNELS))
    )


async def _update_status_channels():
    if not _STATUS_CHANNELS:
        return
    loop = asyncio.get_running_loop()
    recent_error = bool(_LAST_LOG_ERROR_AT and loop.time() - _LAST_LOG_ERROR_AT < 600)
    dashboard_ok = False
    if DASHBOARD_URL.startswith("https://") and DASHBOARD_INGEST_KEY:
        try:
            timeout = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{DASHBOARD_URL}/health") as response:
                    dashboard_ok = response.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            dashboard_ok = False
    configured = bool(DASHBOARD_URL and DASHBOARD_INGEST_KEY)
    healthy = (dashboard_ok or not configured) and not recent_error
    emoji = "🔴" if not client.is_ready() else ("🟢" if healthy else "🟠")
    for guild_id, channel in list(_STATUS_CHANNELS.items()):
        if channel.guild is None:
            continue
        wanted = emoji + STATUS_CHANNEL_SUFFIX
        if channel.name != wanted:
            try:
                await channel.edit(name=wanted, reason="Refresh Social Watch health indicator")
            except discord.DiscordException as error:
                builtins.print(f"Could not update status channel in {channel.guild.name}: {type(error).__name__}")


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



# Infrastructure operations are deliberately restricted to explicit admin actions.
_infrastructure = InfrastructureManager.from_environment()


@management_group.command(name="infrastructure", description="Manage connected hosting services")
@app_commands.describe(
    operation="Action to perform on configured non-database services",
    confirm="Required for redeploy, restart, shutdown, and start operations",
)
@app_commands.choices(operation=[
    app_commands.Choice(name="Status", value="status"),
    app_commands.Choice(name="Deploy latest source", value="deploy"),
    app_commands.Choice(name="Redeploy current build", value="redeploy"),
    app_commands.Choice(name="Restart services", value="restart"),
    app_commands.Choice(name="Shutdown services", value="shutdown"),
    app_commands.Choice(name="Start / open services", value="start"),
])
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_infrastructure(
    interaction: discord.Interaction,
    operation: app_commands.Choice[str],
    confirm: bool = False,
):
    action = operation.value
    if action != "status" and not confirm:
        return await management_denied(
            interaction,
            "Safety check: set **confirm = True** to run this infrastructure action. "
            "Only explicitly configured managed service IDs are affected; databases are not included automatically.",
        )
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await _infrastructure.run(action)
        await interaction.followup.send(result[:1900], ephemeral=True)
    except InfrastructureError as error:
        await interaction.followup.send(f"Infrastructure action not completed: {error}"[:1900], ephemeral=True)
    except Exception as error:
        print(f"Infrastructure action failed ({action}): {type(error).__name__}")
        await interaction.followup.send(
            "Infrastructure action failed. Check the Social Watch logs; no success is being assumed.",
            ephemeral=True,
        )



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
                action = str(rule.get("action", "discord_nest")).lower()
                if action not in {"record_only", "discord", "nest", "discord_nest", "redeploy"}:
                    action = "discord_nest"
                match_mode = str(rule.get("matchMode", "contains")).lower()
                if match_mode not in {"contains", "whole_word"}:
                    match_mode = "contains"
                exclude = rule.get("exclude", [])
                if not isinstance(exclude, list):
                    exclude = []
                clean_rules.append({
                    "term": term,
                    "priority": priority,
                    "action": action,
                    "matchMode": match_mode,
                    "exclude": [str(item).strip().lower()[:80] for item in exclude[:20] if str(item).strip()],
                })
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
        await _update_status_channels()
        await asyncio.sleep(60)


@client.event
async def on_disconnect():
    # While the process is still alive, try to mark every status VC red if the bot disconnects.
    await _update_status_channels()


async def call_home_assistant(service_domain, service_name, data):
    """Call a Home Assistant service using a long-lived access token."""
    if not HOME_ASSISTANT_URL.startswith("https://") or not HOME_ASSISTANT_TOKEN:
        return False, "Home Assistant API is not configured. Set HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN in Railway."
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{HOME_ASSISTANT_URL}/api/services/{service_domain}/{service_name}",
                headers={"Authorization": f"Bearer {HOME_ASSISTANT_TOKEN}", "Content-Type": "application/json"},
                json=data,
            ) as response:
                if response.status in (200, 201):
                    return True, "Home Assistant accepted the command."
                return False, f"Home Assistant returned HTTP {response.status}. Check the service, token, and entity ID."
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False, "Home Assistant API could not be reached. Check its HTTPS URL and network access."


class NestVolumeModal(discord.ui.Modal, title="Set Nest volume"):
    volume = discord.ui.TextInput(label="Volume (0 to 100)", placeholder="Example: 35", min_length=1, max_length=3, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not NEST_MEDIA_PLAYER_ENTITY_ID:
            return await interaction.response.send_message("Set NEST_MEDIA_PLAYER_ENTITY_ID in Railway first.", ephemeral=True)
        try:
            value = int(self.volume.value)
            if value < 0 or value > 100:
                raise ValueError()
        except ValueError:
            return await interaction.response.send_message("Enter a whole number from 0 to 100.", ephemeral=True)
        ok, message = await call_home_assistant("media_player", "volume_set", {
            "entity_id": NEST_MEDIA_PLAYER_ENTITY_ID, "volume_level": value / 100,
        })
        await interaction.response.send_message(("✅ " if ok else "⚠️ ") + message, ephemeral=True)


class NestAnnouncementModal(discord.ui.Modal, title="Speak on Nest Hub"):
    message = discord.ui.TextInput(label="Announcement text", style=discord.TextStyle.paragraph, max_length=220, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not NEST_MEDIA_PLAYER_ENTITY_ID:
            return await interaction.response.send_message("Set NEST_MEDIA_PLAYER_ENTITY_ID in Railway first.", ephemeral=True)
        ok, result = await call_home_assistant("tts", "speak", {
            "entity_id": "tts.google_translate_en_com",
            "media_player_entity_id": NEST_MEDIA_PLAYER_ENTITY_ID,
            "message": self.message.value,
        })
        await interaction.response.send_message(("✅ " if ok else "⚠️ ") + result, ephemeral=True)


class NestAlarmModal(discord.ui.Modal, title="Set a Nest alarm"):
    command = discord.ui.TextInput(
        label="Alarm request",
        placeholder="Example: Set an alarm for 7:00 AM tomorrow",
        style=discord.TextStyle.paragraph, max_length=160, required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # This uses Home Assistant's Google Assistant SDK integration if installed.
        # Google Assistant may apply the alarm to its configured/default device, not necessarily this Hub.
        ok, result = await call_home_assistant("google_assistant_sdk", "send_text_command", {
            "command": str(self.command.value).strip(),
        })
        if ok:
            result += " Check the Nest Hub to confirm the alarm was created on the intended device; Assistant SDK may use its configured default device."
        else:
            result += " This requires the Google Assistant SDK integration in Home Assistant. If unavailable, set alarms by voice on the Nest Hub."
        await interaction.response.send_message(("✅ " if ok else "⚠️ ") + result, ephemeral=True)


class NestControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    async def _media(self, interaction, service, data=None):
        if not NEST_MEDIA_PLAYER_ENTITY_ID:
            return await interaction.response.send_message("Set NEST_MEDIA_PLAYER_ENTITY_ID in Railway first (example: media_player.kitchen_display).", ephemeral=True)
        payload = {"entity_id": NEST_MEDIA_PLAYER_ENTITY_ID}
        if data:
            payload.update(data)
        ok, result = await call_home_assistant("media_player", service, payload)
        await interaction.response.send_message(("✅ " if ok else "⚠️ ") + result, ephemeral=True)

    @discord.ui.button(label="VOLUME −", style=discord.ButtonStyle.secondary, row=0, custom_id="socialwatch:nest:volume_down")
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._media(interaction, "volume_down")

    @discord.ui.button(label="VOLUME +", style=discord.ButtonStyle.secondary, row=0, custom_id="socialwatch:nest:volume_up")
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._media(interaction, "volume_up")

    @discord.ui.button(label="MUTE", style=discord.ButtonStyle.secondary, row=0, custom_id="socialwatch:nest:mute")
    async def mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._media(interaction, "volume_mute", {"is_volume_muted": True})

    @discord.ui.button(label="UNMUTE", style=discord.ButtonStyle.secondary, row=0, custom_id="socialwatch:nest:unmute")
    async def unmute(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._media(interaction, "volume_mute", {"is_volume_muted": False})

    @discord.ui.button(label="SET VOLUME", style=discord.ButtonStyle.primary, row=1, custom_id="socialwatch:nest:set_volume")
    async def set_volume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NestVolumeModal())

    @discord.ui.button(label="SPEAK MESSAGE", style=discord.ButtonStyle.primary, row=1, custom_id="socialwatch:nest:speak")
    async def speak(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NestAnnouncementModal())

    @discord.ui.button(label="SET ALARM", style=discord.ButtonStyle.success, row=1, custom_id="socialwatch:nest:alarm")
    async def alarm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NestAlarmModal())


class AnnouncementModal(discord.ui.Modal, title="Post an announcement"):
    announcement = discord.ui.TextInput(label="Announcement", style=discord.TextStyle.paragraph, max_length=1800, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or str(interaction.user.id) not in ADMIN_USER_IDS:
            return await interaction.response.send_message("Only an allowlisted Social Watch admin can post announcements.", ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in the announcements channel.", ephemeral=True)
        try:
            await interaction.channel.send("📣 ANNOUNCEMENT\n" + self.announcement.value, allowed_mentions=discord.AllowedMentions.none())
            await interaction.response.send_message("Announcement posted.", ephemeral=True)
        except discord.DiscordException as error:
            await interaction.response.send_message("Announcement could not be posted: " + type(error).__name__, ephemeral=True)


class KeywordTestModal(discord.ui.Modal, title="Test watched keywords"):
    test_message = discord.ui.TextInput(label="Message to test", style=discord.TextStyle.paragraph, max_length=1500, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        sample = str(self.test_message.value).lower()
        matches = []
        for rule in KEYWORD_RULES:
            term = str(rule.get("term", "")).strip().lower()
            if not term:
                continue
            found = bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", sample)) if rule.get("matchMode") == "whole_word" else term in sample
            exclusions = rule.get("exclude", [])
            if found and not any(str(item).lower() in sample for item in exclusions):
                matches.append("• " + term + " — " + str(rule.get("priority", "normal")) + " priority / " + str(rule.get("action", "discord_nest")))
        await interaction.response.send_message("\n".join(matches[:15]) if matches else "No active keyword rules matched that sample.", ephemeral=True)


PANEL_CONFIG = {
    "welcome": ("OPEN QUICK GUIDE", "START HERE: Social Watch monitors configured sources, evaluates watched keyword rules, and routes matching alerts. Use the quick guide for setup and safety notes."),
    "role-chooser": ("CHOOSE YOUR SETUP", "Choose House Functions or Phone Functions. You can switch sections later; the selected role controls which private category is visible."),
    "announcements": ("POST ANNOUNCEMENT", "Admin-only announcements for confirmed service changes, maintenance notices, and important updates. Never post secrets."),
    "system-status": ("CHECK SYSTEM STATUS", "Check whether the Discord bot and dashboard respond. A healthy status does not guarantee every external integration is working."),
    "live-alerts": ("TOGGLE LIVE ALERTS", "Pause or resume Discord alert delivery for the current bot process. Verify saved dashboard settings after a restart."),
    "social-feed": ("OPEN DASHBOARD", "Open the Social Watch dashboard to review alert history, configure keyword rules, and manage notification preferences."),
    "keyword-matches": ("TEST KEYWORDS", "Test sample text against active rules. Check match mode, exclusions, priority, and selected destination."),
    "bot-commands": ("COMMAND GUIDE", "Review available Social Watch slash commands. Administrative actions require an authorized admin and relevant bot permissions."),
    "automation-settings": ("VIEW SETTINGS", "Review Discord alerts, Home Assistant announcements, Nest mode, and quiet hours. Runtime-only controls may reset after restart."),
    "integration-status": ("CHECK INTEGRATIONS", "Check dashboard reachability and Home Assistant webhook configuration. Configured does not mean a real Nest announcement has been tested."),
    "event-logs": ("RECENT EVENTS", "Review recent messages in this channel. This is not a complete audit of every external event."),
    "error-logs": ("RECENT ERRORS", "Filter this channel for error-like messages. Check Railway logs for detailed deployment and runtime diagnostics."),
    "audit-log": ("RECORD PANEL USE", "Write a lightweight panel-use entry. This is not a tamper-proof security audit log."),
    "house-dashboard": ("CHECK HOME STATUS", "HOUSE DASHBOARD: Home automation overview and setup notes. Connect and test integrations before allowing them to control devices."),
    "house-notifications": ("VIEW ALERT SETTINGS", "HOUSE NOTIFICATIONS: Home-related alert preferences and summaries. Configure Home Assistant before expecting announcements on your Nest Hub."),
    "nest-controls": ("SEND TEST NEST ALERT", "NEST CONTROLS: Google Nest announcements run through Home Assistant. This channel does not control the display until the Home Assistant automation is configured."),
    "home-automations": ("VIEW AUTOMATION STATUS", "HOME AUTOMATIONS: Document routines, triggers, conditions, and actions here. Test automations safely before enabling them."),
    "phone-socials": ("CHECK SOCIAL SOURCES", "PHONE & SOCIALS: Track supported social integrations and connection status. Only connected sources can generate live monitoring events."),
    "phone-notifications": ("VIEW PHONE ALERTS", "PHONE NOTIFICATIONS: Review notification routing and priorities. Do not post authentication codes or sensitive personal messages here."),
    "keyword-settings": ("OPEN KEYWORD SETTINGS", "KEYWORD SETTINGS: Manage watched words and phrases in the dashboard. Test contains vs whole-word matching, exclusions, priority, and routing."),
    "phone-automations": ("VIEW PHONE AUTOMATIONS", "PHONE AUTOMATIONS: Document supported phone-related triggers and actions. iPhone app notifications alone do not automatically send message content to the cloud bot."),
}


class RoleChooserPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        house = discord.ui.Button(label="HOUSE FUNCTIONS", style=discord.ButtonStyle.success, custom_id="socialwatch:role:house")
        phone = discord.ui.Button(label="PHONE FUNCTIONS", style=discord.ButtonStyle.primary, custom_id="socialwatch:role:phone")
        house.callback = self.choose_house
        phone.callback = self.choose_phone
        self.add_item(house)
        self.add_item(phone)

    async def _assign(self, interaction: discord.Interaction, selected: str):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this button inside the Social Watch server.", ephemeral=True)
        role_names = {"house": "House", "phone": "Phone"}
        chosen = discord.utils.get(guild.roles, name=role_names[selected])
        other = discord.utils.get(guild.roles, name=role_names["phone" if selected == "house" else "house"])
        if chosen is None:
            return await interaction.response.send_message("That role hasn't been created yet. Ask a server admin to check the bot's Manage Roles permission.", ephemeral=True)
        try:
            if other and other in interaction.user.roles:
                await interaction.user.remove_roles(other, reason="Social Watch role chooser selection")
            if chosen not in interaction.user.roles:
                await interaction.user.add_roles(chosen, reason="Social Watch role chooser selection")
        except discord.Forbidden:
            return await interaction.response.send_message("I can't assign roles yet. An admin must give the bot Manage Roles and move its bot role above House and Phone.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.response.send_message("Discord couldn't update your role. Try again in a moment.", ephemeral=True)
        section = "House Functions" if selected == "house" else "Phone Functions"
        return await interaction.response.send_message("You're set for **" + section + "**. That section should now appear in the channel list. You can return here and switch at any time.", ephemeral=True)

    async def choose_house(self, interaction: discord.Interaction):
        await self._assign(interaction, "house")

    async def choose_phone(self, interaction: discord.Interaction):
        await self._assign(interaction, "phone")


class SocialWatchPanel(discord.ui.View):
    def __init__(self, panel_key: str):
        super().__init__(timeout=None)
        self.panel_key = panel_key
        label, _description = PANEL_CONFIG[panel_key]
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.success, custom_id="socialwatch:panel:" + panel_key)
        button.callback = self.run_action
        self.add_item(button)

    async def run_action(self, interaction: discord.Interaction):
        key = self.panel_key
        channel = interaction.channel
        if key == "role-chooser":
            return await interaction.response.send_message("Choose the section you want visible: **House Functions** for home/Nest/automation controls, or **Phone Functions** for social monitoring, keyword alerts, and phone notifications. Choosing one removes the other role; you can switch whenever you want.", ephemeral=True)
        if key == "welcome":
            guide = (
                "**SOCIAL WATCH — QUICK START**\n\n"
                "**1. Choose your section**\nUse the buttons in #role-chooser to show either House Functions or Phone Functions. You can switch later.\n\n"
                "**2. Watched keywords**\nOpen the dashboard and add words or phrases in keyword settings. Choose priority and action, then save. Contains matches a phrase anywhere; whole word avoids matches inside larger words. Add exclusions to ignore messages containing certain phrases.\n\n"
                "**3. Alerts**\nMatching messages are recorded in dashboard history. Depending on each rule's action, alerts may go to Discord, Google Nest through Home Assistant, or both. Record-only rules save matches without sending those alerts.\n\n"
                "**4. Channel buttons**\nUse each pinned channel button for its matching action. Check #system-status and #integration-status if something isn't working.\n\n"
                "**Important:** keep the bot running on Railway and never share bot tokens or webhook secrets."
            )
            return await interaction.response.send_message(guide, ephemeral=True)
        if key == "announcements":
            if str(interaction.user.id) not in ADMIN_USER_IDS:
                return await interaction.response.send_message("Only an allowlisted Social Watch admin can post announcements.", ephemeral=True)
            return await interaction.response.send_modal(AnnouncementModal())
        if key == "keyword-matches":
            return await interaction.response.send_modal(KeywordTestModal())
        if key == "live-alerts":
            AUTOMATION_SETTINGS["discordChannelAlertsEnabled"] = not AUTOMATION_SETTINGS.get("discordChannelAlertsEnabled", True)
            state = "ON" if AUTOMATION_SETTINGS["discordChannelAlertsEnabled"] else "PAUSED"
            return await interaction.response.send_message("Discord alert delivery is now " + state + " for this bot process. This toggle may reset after restart or settings sync.", ephemeral=True)
        if key in {"system-status", "integration-status"}:
            dashboard_ok = False
            if DASHBOARD_URL.startswith("https://"):
                try:
                    timeout = aiohttp.ClientTimeout(total=4)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(DASHBOARD_URL + "/health") as response:
                            dashboard_ok = response.status == 200
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
            if key == "system-status":
                result = "Bot: " + ("online" if client.is_ready() else "offline") + "\nDashboard: " + ("healthy" if dashboard_ok else "unreachable / not configured")
            else:
                result = "Dashboard: " + ("reachable" if dashboard_ok else "unreachable / not configured") + "\nHome Assistant webhook: " + ("configured" if HOME_ASSISTANT_WEBHOOK_URL.startswith("https://") else "not configured")
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "house-dashboard":
            dashboard_ok = False
            if DASHBOARD_URL.startswith("https://"):
                try:
                    timeout = aiohttp.ClientTimeout(total=4)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(DASHBOARD_URL + "/health") as response:
                            dashboard_ok = response.status == 200
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
            result = "**Home status**\nDashboard: " + ("online" if dashboard_ok else "unreachable")
            result += "\nHome Assistant webhook: " + ("configured" if HOME_ASSISTANT_WEBHOOK_URL.startswith("https://") else "not configured")
            result += "\n\nDashboard: " + (DASHBOARD_URL or "DASHBOARD_URL is not set in Railway")
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "house-notifications":
            settings = AUTOMATION_SETTINGS
            result = "**Home notification settings**\nHome Assistant announcements: " + ("enabled" if settings.get("homeAssistantAnnouncementsEnabled", True) else "disabled")
            result += "\nNest announcement mode: " + str(settings.get("nestAnnouncementMode", "all"))
            result += "\nQuiet hours: " + ("enabled" if settings.get("quietHoursEnabled") else "disabled")
            result += "\nWindow: " + str(settings.get("quietHoursStart", "22:00")) + "–" + str(settings.get("quietHoursEnd", "07:00")) + " (" + str(settings.get("timezone", "America/New_York")) + ")"
            result += "\n\nEdit saved preferences in the dashboard: " + (DASHBOARD_URL or "dashboard URL not configured")
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "nest-controls":
            return await interaction.response.send_message(
                "**Google Nest Hub controls**\nChoose a control below. Direct controls require the Home Assistant API, a long-lived access token, and your Nest media-player entity ID. Alarm creation additionally requires the Google Assistant SDK integration and may use its default device.\n\n"
                + "Webhook test: " + ("configured" if HOME_ASSISTANT_WEBHOOK_URL.startswith("https://") else "not configured")
                + "\nHome Assistant API: " + ("configured" if HOME_ASSISTANT_URL.startswith("https://") and HOME_ASSISTANT_TOKEN else "not configured")
                + "\nNest entity: " + (NEST_MEDIA_PLAYER_ENTITY_ID or "not configured"),
                view=NestControlsView(), ephemeral=True
            )
        if key == "home-automations":
            settings = AUTOMATION_SETTINGS
            result = "**Home automation status**\nHome Assistant webhook: " + ("configured" if HOME_ASSISTANT_WEBHOOK_URL.startswith("https://") else "not configured")
            result += "\nAnnouncements: " + ("enabled" if settings.get("homeAssistantAnnouncementsEnabled", True) else "disabled")
            result += "\nNest mode: " + str(settings.get("nestAnnouncementMode", "all"))
            result += "\n\nThe bot currently forwards qualifying keyword alerts. Device-control routines must be created and tested in Home Assistant separately."
            result += "\nDashboard: " + (DASHBOARD_URL or "not configured")
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "phone-socials":
            result = "**Connected monitoring source**\nDiscord server messages: monitored for configured watched keywords where message content is accessible to the bot."
            result += "\nExternal social networks and TextNow: not automatically connected by this bot unless a supported integration has been configured."
            result += "\nDashboard: " + (DASHBOARD_URL or "not configured")
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "phone-notifications":
            settings = AUTOMATION_SETTINGS
            result = "**Phone notification routing**\nDiscord alerts: " + ("enabled" if settings.get("discordChannelAlertsEnabled", True) else "paused")
            result += "\nHome Assistant announcements: " + ("enabled" if settings.get("homeAssistantAnnouncementsEnabled", True) else "disabled")
            result += "\nNest mode: " + str(settings.get("nestAnnouncementMode", "all"))
            result += "\nKeyword matches are recorded in dashboard history when dashboard forwarding is configured."
            result += "\nDashboard: " + (DASHBOARD_URL or "not configured")
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "keyword-settings":
            result = "**Keyword settings**\nActive in-memory rules: " + str(len(KEYWORD_RULES))
            result += "\nRules support contains or whole-word matching, exclusions, priority, and destination actions."
            result += "\nEdit and save your rules here: " + (DASHBOARD_URL or "DASHBOARD_URL is not set in Railway")
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "phone-automations":
            result = "**Phone automation status**\nKeyword evaluation: active for accessible Discord messages."
            result += "\nDashboard forwarding: " + ("configured" if DASHBOARD_URL.startswith("https://") and DASHBOARD_INGEST_KEY else "not configured")
            result += "\nTextNow/iPhone SMS and calls: no direct event feed is configured in this bot."
            result += "\n\nManage watched keywords and routing: " + (DASHBOARD_URL or "dashboard URL not configured")
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "social-feed":
            return await interaction.response.send_message("Social Watch dashboard: " + (DASHBOARD_URL or "not configured in Railway"), ephemeral=True)
        if key == "bot-commands":
            return await interaction.response.send_message("Commands: /socialwatch channels, /socialwatch infrastructure, /socialwatch create-channel, /socialwatch create-category, /socialwatch create-role. Management actions require an allowlisted admin.", ephemeral=True)
        if key == "automation-settings":
            settings = AUTOMATION_SETTINGS
            result = "Discord alerts: " + ("enabled" if settings.get("discordChannelAlertsEnabled") else "paused")
            result += "\nHome Assistant announcements: " + ("enabled" if settings.get("homeAssistantAnnouncementsEnabled") else "disabled")
            result += "\nNest mode: " + str(settings.get("nestAnnouncementMode", "unknown"))
            result += "\nQuiet hours: " + ("enabled" if settings.get("quietHoursEnabled") else "disabled") + " (" + str(settings.get("quietHoursStart", "22:00")) + "–" + str(settings.get("quietHoursEnd", "07:00")) + " " + str(settings.get("timezone", "America/New_York")) + ")"
            return await interaction.response.send_message(result, ephemeral=True)
        if key == "audit-log" and isinstance(channel, discord.TextChannel):
            try:
                await channel.send("Panel used by " + str(interaction.user) + " (ID " + str(interaction.user.id) + ") at " + datetime.now().astimezone().isoformat(timespec="seconds"), allowed_mentions=discord.AllowedMentions.none())
            except discord.DiscordException:
                pass
            return await interaction.response.send_message("Audit entry attempted in this channel.", ephemeral=True)
        if key in {"event-logs", "error-logs"} and isinstance(channel, discord.TextChannel):
            entries = []
            async for message in channel.history(limit=30):
                if message.author.id == client.user.id and "SOCIAL_WATCH_PANEL:" in message.content:
                    continue
                value = message.content.strip().replace("\n", " ")
                if key == "error-logs" and not any(word in value.lower() for word in ("error", "failed", "failure", "exception", "critical", "traceback", "denied")):
                    continue
                if value:
                    entries.append("• " + message.created_at.strftime("%m-%d %H:%M") + " — " + value[:180])
                if len(entries) >= 8:
                    break
            heading = "Recent errors" if key == "error-logs" else "Recent events"
            return await interaction.response.send_message(heading + "\n" + ("\n".join(entries) if entries else "No matching messages found in this channel yet."), ephemeral=True)
        return await interaction.response.send_message("Social Watch quick guide: use the pinned button in each channel for its matching action. Keyword testing is in keyword-matches; status checks are in system-status; integrations are in integration-status.", ephemeral=True)


async def _ensure_channel_panels(guild):
    created = 0
    for channel_name, (button_label, description) in PANEL_CONFIG.items():
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel is None:
            continue
        view = RoleChooserPanel() if channel_name == "role-chooser" else SocialWatchPanel(channel_name)
        client.add_view(view)
        marker = "SOCIAL_WATCH_PANEL:" + channel_name
        existing = None
        try:
            async for message in channel.history(limit=50):
                if message.author.id == client.user.id and marker in message.content:
                    existing = message
                    break
        except discord.DiscordException as error:
            print("Panel lookup failed in #" + channel_name + ": " + type(error).__name__)
            continue
        if existing is None:
            try:
                message = await channel.send("🟢 SOCIAL WATCH · " + channel_name.upper() + "\n" + description + "\n\nPress the button below whenever you need this action.\n" + marker, view=view, allowed_mentions=discord.AllowedMentions.none())
                try:
                    await message.pin(reason="Persistent Social Watch channel action panel")
                except discord.DiscordException as error:
                    print("Could not pin panel in #" + channel_name + ": " + type(error).__name__)
                created += 1
                # Avoid burst-posting panels into 12 channels at startup.
                await asyncio.sleep(2)
            except discord.DiscordException as error:
                builtins.print("Panel creation failed in #" + channel_name + ": " + type(error).__name__)
        else:
            try:
                await existing.edit(view=view)
            except discord.DiscordException as error:
                print("Panel refresh failed in #" + channel_name + ": " + type(error).__name__)
    print("Social Watch channel panels ready in " + guild.name + ": created=" + str(created) + ", total=" + str(len(PANEL_CONFIG)))


async def _apply_server_layout(guild):
    """Idempotently add the Social Watch layout without deleting existing server assets."""
    # Create selectable section roles first so category permissions can reference them.
    for role_name, color_value in [("House", 0x39FF14), ("Phone", 0x20D840)]:
        if discord.utils.get(guild.roles, name=role_name) is None:
            try:
                await guild.create_role(name=role_name, colour=discord.Colour(color_value), reason="Social Watch section chooser")
            except discord.HTTPException as error:
                print("Could not create " + role_name + " role: " + type(error).__name__)
    layout = {
        "▰ COMMAND CENTER": ["welcome", "role-chooser", "announcements", "system-status"],
        "▰ SOCIAL MONITORING": ["live-alerts", "social-feed", "keyword-matches"],
        "▰ AUTOMATION": ["bot-commands", "automation-settings", "integration-status"],
        "▰ HOUSE FUNCTIONS": ["house-dashboard", "house-notifications", "nest-controls", "home-automations"],
        "▰ PHONE FUNCTIONS": ["phone-socials", "phone-notifications", "keyword-settings", "phone-automations"],
        "▰ SYSTEM LOGS": ["event-logs", "error-logs", "audit-log"],
    }
    made_categories = 0
    made_channels = 0
    issues = []
    for category_name, channel_names in layout.items():
        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            # discord.py expects a dict for overwrites; an empty dict means default permissions.
            overwrites = {}
            if category_name in {"▰ HOUSE FUNCTIONS", "▰ PHONE FUNCTIONS"}:
                selected_name = "House" if category_name == "▰ HOUSE FUNCTIONS" else "Phone"
                selected_role = discord.utils.get(guild.roles, name=selected_name)
                overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
                if selected_role:
                    overwrites[selected_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
                for admin_role_name in ("Owner", "Administrator", "Automation"):
                    admin_role = discord.utils.get(guild.roles, name=admin_role_name)
                    if admin_role:
                        overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
                if guild.me:
                    overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            if category_name == "▰ SYSTEM LOGS":
                overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
                if guild.me:
                    overwrites[guild.me] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True
                    )
                for user_id in ADMIN_USER_IDS:
                    member = guild.get_member(int(user_id))
                    if member is None:
                        try:
                            member = await guild.fetch_member(int(user_id))
                        except discord.DiscordException:
                            member = None
                    if member:
                        overwrites[member] = discord.PermissionOverwrite(
                            view_channel=True, send_messages=True, read_message_history=True
                        )
            try:
                category = await guild.create_category(
                    category_name, overwrites=overwrites,
                    reason="Social Watch black and neon-green server rebuild"
                )
                made_categories += 1
            except discord.HTTPException as error:
                issues.append(f"{category_name}: {type(error).__name__}")
                continue
        if category_name in {"▰ HOUSE FUNCTIONS", "▰ PHONE FUNCTIONS"}:
            selected_name = "House" if category_name == "▰ HOUSE FUNCTIONS" else "Phone"
            selected_role = discord.utils.get(guild.roles, name=selected_name)
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            if selected_role:
                overwrites[selected_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            for admin_role_name in ("Owner", "Administrator", "Automation"):
                admin_role = discord.utils.get(guild.roles, name=admin_role_name)
                if admin_role:
                    overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            if guild.me:
                overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            try:
                await category.edit(overwrites=overwrites, reason="Apply Social Watch section role visibility")
            except discord.HTTPException as error:
                issues.append(f"{category_name} permissions: {type(error).__name__}")
        for channel_name in channel_names:
            existing_channel = discord.utils.find(
                lambda ch: ch.name == channel_name and ch.category_id == category.id,
                guild.text_channels,
            )
            if existing_channel:
                continue
            try:
                created_channel = await guild.create_text_channel(
                    channel_name, category=category,
                    topic="Social Watch black and neon-green control center.",
                    reason="Social Watch black and neon-green server rebuild"
                )
                if channel_name == "role-chooser":
                    welcome_channel = discord.utils.find(
                        lambda ch: ch.name == "welcome" and ch.category_id == category.id,
                        guild.text_channels,
                    )
                    if welcome_channel:
                        await created_channel.edit(position=welcome_channel.position + 1)
                made_channels += 1
            except discord.HTTPException as error:
                issues.append(f"{channel_name}: {type(error).__name__}")
    for role_name, color_value in [
        ("Owner", 0x39FF14), ("Administrator", 0x20D840),
        ("Moderator", 0x168A32), ("Automation", 0x526B58), ("Member", 0x252B27)
    ]:
        if discord.utils.get(guild.roles, name=role_name) is None:
            try:
                await guild.create_role(
                    name=role_name, colour=discord.Colour(color_value),
                    reason="Social Watch black and neon-green server rebuild"
                )
            except discord.HTTPException as error:
                issues.append(f"Role {role_name}: {type(error).__name__}")
    print(
        f"Server layout applied in {guild.name}: categories={made_categories}, "
        f"channels={made_channels}, issues={len(issues)}"
    )
    for issue in issues[:10]:
        print("Server layout issue: " + issue)
    return made_categories, made_channels, issues


@client.event
async def on_ready():
    global _CONFIG_SYNC_TASK

    print(f"Social Watch is online as {client.user}")
    await _ensure_control_channels()

    # The user requested the rebuild directly; apply the additive, repeat-safe layout on startup.
    for server in client.guilds:
        if bot_has_guild_permission(server, "manage_channels"):
            try:
                await _apply_server_layout(server)
                await _ensure_channel_panels(server)
            except discord.DiscordException as error:
                print(f"Automatic server layout failed in {server.name}: {type(error).__name__}")
        else:
            print(f"Automatic server layout skipped in {server.name}: Manage Channels permission missing.")

    try:
        # Sync slash commands directly to both the management server and the
        # specifically approved RacksArchive server so new guild-only commands
        # appear immediately in both places.
        sync_guild_ids = {1552866429865558086}
        if MANAGEMENT_GUILD_ID.isdigit():
            sync_guild_ids.add(int(MANAGEMENT_GUILD_ID))
        if sync_guild_ids:
            for guild_id in sorted(sync_guild_ids):
                target_guild = discord.Object(id=guild_id)
                tree.copy_global_to(guild=target_guild)
                synced = await tree.sync(guild=target_guild)
                print(f"Synced {len(synced)} management commands to guild {guild_id}.")
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
        await post_integration_heartbeat("home_assistant", "configured")
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


# Discord can redeliver gateway events after reconnects. Keep a bounded cache so one
# source message cannot create duplicate alerts during the same bot process.
_SEEN_MESSAGE_IDS = set()
_SEEN_MESSAGE_ORDER = []

@client.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    message_id = message.id
    if message_id in _SEEN_MESSAGE_IDS:
        return
    _SEEN_MESSAGE_IDS.add(message_id)
    _SEEN_MESSAGE_ORDER.append(message_id)
    if len(_SEEN_MESSAGE_ORDER) > 5000:
        expired_id = _SEEN_MESSAGE_ORDER.pop(0)
        _SEEN_MESSAGE_IDS.discard(expired_id)

    content = message.content.lower()

    def matches_rule(rule):
        term = rule["term"]
        mode = rule.get("matchMode", "contains")
        if mode == "whole_word":
            matched = re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", content) is not None
        else:
            matched = term in content
        if not matched:
            return False
        excluded = rule.get("exclude", [])
        return not any(item and item in content for item in excluded)

    matches = [rule for rule in KEYWORD_RULES if matches_rule(rule)]
    if not matches:
        return

    matched_terms = [rule["term"] for rule in matches]
    priority = get_priority(matches)
    actions = {rule.get("action", "discord_nest") for rule in matches}

    # Route each rule only to its own destinations. Dashboard history records every match.
    discord_matches = [
        rule for rule in matches
        if rule.get("action", "discord_nest") in {"discord", "discord_nest"}
    ]
    nest_matches = [
        rule for rule in matches
        if rule.get("action", "discord_nest") in {"nest", "discord_nest"}
    ]

    print(
        f"Keyword alert | Server: {message.guild.name} "
        f"| Channel: {message.channel} "
        f"| Priority: {priority} "
        f"| Matches: {', '.join(matched_terms)}"
    )

    # Infrastructure actions never execute from a casual keyword match.
    # Require an allowlisted admin and an exact confirmation phrase.
    if "redeploy" in actions:
        admin_confirmed = (
            str(message.author.id) in ADMIN_USER_IDS
            and content.strip() == "project update confirm redeploy"
        )
        if admin_confirmed:
            try:
                result = await _infrastructure.run("redeploy")
                print("Admin-confirmed keyword redeployment completed: " + result)
            except InfrastructureError as error:
                print("Admin-confirmed keyword redeployment failed: " + str(error))
        else:
            print("Redeploy keyword matched; ignored because admin confirmation phrase was not exact.")

    if discord_matches and AUTOMATION_SETTINGS.get("discordChannelAlertsEnabled", True):
        alert_channel = client.get_channel(int(ALERT_CHANNEL_ID))
        if alert_channel is None:
            try:
                alert_channel = await client.fetch_channel(int(ALERT_CHANNEL_ID))
            except discord.HTTPException as error:
                print(f"Could not find Discord alert channel: {error}")
                alert_channel = None

        if alert_channel is not None:
            channel_name = getattr(message.channel, "name", str(message.channel))
            discord_terms = ", ".join(rule["term"] for rule in discord_matches)
            discord_priority = get_priority(discord_matches)
            alert = (
                "**Social Watch Alert**\n"
                f"**Priority:** {discord_priority.upper()}\n"
                f"**Server:** {message.guild.name}\n"
                f"**Channel:** #{channel_name}\n"
                f"**Author:** {message.author}\n"
                f"**Matched keywords:** {discord_terms}\n"
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
    elif discord_matches:
        print("Discord channel notifications are disabled in dashboard settings.")
    else:
        print("Matched keyword action does not include Discord delivery.")

    # Persist every qualifying match so record-only rules remain visible in history.
    await forward_to_dashboard(matches, message, priority)
    if nest_matches:
        await forward_to_home_assistant(nest_matches, message, get_priority(nest_matches))
    else:
        print("Matched keyword action does not include Google Nest delivery.")


@management_group.command(name="rebuild", description="Create the new Social Watch server layout")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True, manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_rebuild(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await management_denied(interaction, "Use this command inside your server.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    layout = {
        "▰ COMMAND CENTER": ["welcome", "announcements", "system-status"],
        "▰ SOCIAL MONITORING": ["live-alerts", "social-feed", "keyword-matches"],
        "▰ AUTOMATION": ["bot-commands", "automation-settings", "integration-status"],
        "▰ SYSTEM LOGS": ["event-logs", "error-logs", "audit-log"],
    }
    made_categories = 0
    made_channels = 0
    issues = []
    for category_name, channel_names in layout.items():
        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            try:
                category = await guild.create_category(category_name, reason="Social Watch server rebuild")
                made_categories += 1
            except discord.HTTPException as error:
                issues.append(f"{category_name}: {type(error).__name__}")
                continue
        for channel_name in channel_names:
            if discord.utils.find(lambda ch: ch.name == channel_name and ch.category_id == category.id, guild.text_channels):
                continue
            try:
                await guild.create_text_channel(channel_name, category=category, reason="Social Watch server rebuild")
                made_channels += 1
            except discord.HTTPException as error:
                issues.append(f"{channel_name}: {type(error).__name__}")
    for role_name, color_value in [("Owner", 0x39FF14), ("Administrator", 0x20D840), ("Moderator", 0x168A32), ("Automation", 0x526B58), ("Member", 0x252B27)]:
        if discord.utils.get(guild.roles, name=role_name) is None:
            try:
                await guild.create_role(name=role_name, colour=discord.Colour(color_value), reason="Social Watch server rebuild")
            except discord.HTTPException as error:
                issues.append(f"Role {role_name}: {type(error).__name__}")
    result = f"Social Watch rebuild finished. Categories: {made_categories}; channels: {made_channels}. Nothing existing was deleted or renamed."
    if issues:
        result += "\nIssues: " + "; ".join(issues[:6])
    await interaction.followup.send(result[:1900], ephemeral=True)


@management_group.command(name="racksarchive", description="Build the RacksArchive Music + Anime server layout")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True, manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_racksarchive(interaction: discord.Interaction):
    """Create a separate music-making hub and anime watch lounge without deleting existing channels."""
    guild = interaction.guild
    if guild is None:
        return await management_denied(interaction, "Use this command inside a server.")
    await interaction.response.defer(ephemeral=True, thinking=True)

    layout = {
        "🎵 RACKS MUSIC LAB": {
            "🎛️・music-lab": "Create beats, discuss DAWs, plugins, drum kits, mixing, and production workflows.",
            "🥁・beat-feedback": "Post beats and get feedback on drums, bounce, melodies, and mix.",
            "🎙️・recording-booth": "Recording tips, vocals, punch-ins, mixing, and mastering.",
            "🎧・song-drops": "Share finished songs, snippets, releases, and music videos you made or have permission to share.",
            "🧑‍🎤・artist-profiles": "Artist profiles, inspirations, releases, and creative goals.",
            "💿・beat-market": "Share original beats and discuss licensing and permissions.",
            "✍️・lyrics-and-writing": "Write lyrics, exchange ideas, and workshop songs.",
            "🤝・music-collabs": "Find producers, rappers, singers, engineers, and collaborators.",
            "📦・racksarchive": "Organize released music, original beats, projects, and authorized archives.",
            "🎬・music-videos": "Share music videos and visualizers you own or have permission to post.",
        },
        "📺 ANIME WORLD": {
            "⭐・anime-recommendations": "Find anime by genre, mood, length, and streaming availability.",
            "📅・seasonal-anime": "Discuss current-season shows and upcoming releases.",
            "🎬・trailers-and-clips": "Official trailers and clips, or content you have permission to share.",
            "🔗・where-to-watch": "Share official streaming services and legal episode links.",
            "🍿・watch-party-events": "Coordinate watch parties and discuss viewing schedules.",
            "📺・watchlists": "Keep personal watchlists and recommendations.",
            "💬・anime-discussion": "Talk about series, characters, studios, and theories.",
            "🗯️・episode-reactions": "React to episodes and new releases; use spoiler tags.",
            "🚨・spoilers": "Major spoilers belong here.",
            "📚・manga-corner": "Manga chapters, adaptations, theories, and reading recommendations.",
        },
        "🌐 RACKSARCHIVE COMMUNITY": {
            "👋・welcome-and-rules": "Welcome to RacksArchive. Read the rules and introduce yourself.",
            "📢・announcements": "RacksArchive community news, events, and updates.",
            "💬・general-chat": "Everyday community chat.",
            "🤖・bot-commands": "Use bot commands here to keep other channels focused.",
        },
    }

    made_categories = 0
    made_channels = 0
    existing_channels = 0
    issues = []
    for category_name, channels in layout.items():
        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            try:
                category = await guild.create_category(
                    category_name, reason="RacksArchive music and anime layout"
                )
                made_categories += 1
            except discord.HTTPException as error:
                issues.append(f"{category_name}: {type(error).__name__}")
                continue
        for channel_name, topic in channels.items():
            existing = discord.utils.find(
                lambda channel: channel.name == channel_name and channel.category_id == category.id,
                guild.text_channels,
            )
            if existing:
                existing_channels += 1
                if existing.topic != topic:
                    try:
                        await existing.edit(topic=topic, reason="Update RacksArchive channel purpose")
                    except discord.HTTPException as error:
                        issues.append(f"{channel_name} topic: {type(error).__name__}")
                continue
            try:
                await guild.create_text_channel(
                    channel_name, category=category, topic=topic,
                    reason="RacksArchive music and anime layout"
                )
                made_channels += 1
            except discord.HTTPException as error:
                issues.append(f"{channel_name}: {type(error).__name__}")

    # Create the two voice rooms separately; repeated runs are safe.
    for category_name, channel_name in [
        ("🎵 MUSIC STUDIO", "🔊・beat-making-vc"),
        ("📺 ANIME LOUNGE", "📺・anime-watch-vc"),
    ]:
        category = discord.utils.get(guild.categories, name=category_name)
        if category and not discord.utils.find(
            lambda channel: channel.name == channel_name and channel.category_id == category.id,
            guild.voice_channels,
        ):
            try:
                await guild.create_voice_channel(
                    channel_name, category=category,
                    reason="RacksArchive voice rooms"
                )
                made_channels += 1
            except discord.HTTPException as error:
                issues.append(f"{channel_name}: {type(error).__name__}")

    result = (
        "**RacksArchive layout finished**\n"
        f"Categories created: {made_categories}\n"
        f"Channels created: {made_channels}\n"
        f"Already present: {existing_channels}\n"
        "Existing channels were not deleted or renamed. Music production and anime viewing/discussion have separate categories."
    )
    if issues:
        result += "\nIssues: " + "; ".join(issues[:8])
    await interaction.followup.send(result[:1900], ephemeral=True)



@management_group.command(name="reset-racksarchive", description="Replace old channels with the planned RacksArchive layout")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True, manage_guild=True)
@app_commands.check(is_authorized_admin)
async def socialwatch_reset_racksarchive(interaction: discord.Interaction):
    """Reset channels only in the specifically approved RacksArchive server; preserve roles and members."""
    target_guild_id = 1552866429865558086
    guild = interaction.guild
    if guild is None:
        return await management_denied(interaction, "Run this inside the new RacksArchive server.")
    if guild.id != target_guild_id:
        return await management_denied(interaction, "Safety stop: this command only works in the approved RacksArchive server. Nothing was changed.")
    if not bot_has_guild_permission(guild, "manage_channels"):
        return await management_denied(interaction, "The bot needs Manage Channels permission.")

    await interaction.response.defer(ephemeral=True, thinking=True)
    deleted = 0
    issues = []
    # Delete only channels/categories in the approved guild. Do not delete roles, members, or server settings.
    for channel in list(guild.channels):
        if isinstance(channel, discord.CategoryChannel):
            continue
        try:
            await channel.delete(reason="User-confirmed RacksArchive-only channel reset")
            deleted += 1
        except discord.HTTPException as error:
            issues.append(f"{channel.name}: {type(error).__name__}")
    for category in list(guild.categories):
        try:
            await category.delete(reason="User-confirmed RacksArchive-only category reset")
        except discord.HTTPException as error:
            issues.append(f"{category.name}: {type(error).__name__}")

    layout = {
        "🎵 RACKS MUSIC LAB": {
            "🎛️・music-lab": "Create beats, discuss DAWs, plugins, drum kits, mixing, and production workflows.",
            "🥁・beat-feedback": "Post beats and get feedback on drums, bounce, melodies, and mix.",
            "🎙️・recording-booth": "Recording tips, vocals, punch-ins, mixing, and mastering.",
            "🎧・song-drops": "Share finished songs, snippets, releases, and music videos you made or have permission to share.",
            "🧑‍🎤・artist-profiles": "Artist profiles, inspirations, releases, and creative goals.",
            "💿・beat-market": "Share original beats and discuss licensing and permissions.",
            "✍️・lyrics-and-writing": "Write lyrics, exchange ideas, and workshop songs.",
            "🤝・music-collabs": "Find producers, rappers, singers, engineers, and collaborators.",
            "📦・racksarchive": "Organize released music, original beats, projects, and authorized archives.",
            "🎬・music-videos": "Share music videos and visualizers you own or have permission to post.",
            "🔊・beat-making-vc": None,
        },
        "📺 ANIME WORLD": {
            "⭐・anime-recommendations": "Find anime by genre, mood, length, and streaming availability.",
            "📅・seasonal-anime": "Discuss current-season shows and upcoming releases.",
            "🎬・trailers-and-clips": "Official trailers and clips, or content you have permission to share.",
            "🔗・where-to-watch": "Share official streaming services and legal episode links.",
            "🍿・watch-party-events": "Coordinate watch parties and discuss viewing schedules.",
            "📺・watchlists": "Keep personal watchlists and recommendations.",
            "💬・anime-discussion": "Talk about series, characters, studios, and theories.",
            "🗯️・episode-reactions": "React to episodes and new releases; use spoiler tags.",
            "🚨・spoilers": "Major spoilers belong here.",
            "📚・manga-corner": "Manga chapters, adaptations, theories, and reading recommendations.",
            "📺・anime-watch-vc": None,
        },
        "🌐 RACKSARCHIVE COMMUNITY": {
            "👋・welcome-and-rules": "Welcome to RacksArchive. Read the rules and introduce yourself.",
            "📢・announcements": "RacksArchive community news, events, and updates.",
            "💬・general-chat": "Everyday community chat.",
            "🤖・bot-commands": "Use bot commands here to keep other channels focused.",
        },
    }
    created_categories = 0
    created_channels = 0
    for category_name, channel_map in layout.items():
        try:
            category = await guild.create_category(category_name, reason="Rebuild planned RacksArchive layout")
            created_categories += 1
        except discord.HTTPException as error:
            issues.append(f"{category_name}: {type(error).__name__}")
            continue
        for channel_name, topic in channel_map.items():
            try:
                if topic is None:
                    await guild.create_voice_channel(channel_name, category=category, reason="Rebuild planned RacksArchive layout")
                else:
                    await guild.create_text_channel(channel_name, category=category, topic=topic, reason="Rebuild planned RacksArchive layout")
                created_channels += 1
            except discord.HTTPException as error:
                issues.append(f"{channel_name}: {type(error).__name__}")

    result = (
        "**RacksArchive reset complete**\\n"
        f"Old channels deleted: {deleted}\\n"
        f"Categories created: {created_categories}\\n"
        f"New channels created: {created_channels}\\n"
        "Roles, members, and server settings were preserved. This command is locked to the approved server ID."
    )
    if issues:
        result += "\\nIssues: " + "; ".join(issues[:8])
    await interaction.followup.send(result[:1900], ephemeral=True)


@management_group.command(name="invite", description="Get a link to add Social Watch to another server")
@app_commands.guild_only()
@app_commands.check(is_authorized_admin)
async def socialwatch_invite(interaction: discord.Interaction):
    if client.user is None:
        return await management_denied(interaction, "The bot identity is not ready yet.")
    permissions = discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=True,
        embed_links=True, attach_files=True, manage_channels=True,
        manage_roles=True, manage_messages=True, connect=True, speak=True,
        use_application_commands=True,
    )
    invite_url = discord.utils.oauth_url(
        client.user.id, permissions=permissions, scopes=("bot", "applications.commands")
    )
    await interaction.response.send_message(
        f"Add Social Watch to a server you manage: {invite_url}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


client.run(TOKEN)
