"""Channel directory -- cached map of reachable channels/contacts per platform.

Built on gateway startup, refreshed every 5 min, saved to ~/.hermes/channel_directory.json.
send_message reads it for action="list" and to resolve friendly channel names to IDs.
"""

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_cli.config import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Paths resolve lazily: a multiplexed gateway serves several profile homes from one
# process, so an import-time constant would pin every profile to whichever home imported
# first. These globals are explicit overrides (tests patch them); None = current home.
DIRECTORY_PATH: Optional[Path] = None
# User-maintained friendly-name overlay {"<platform>": {"<chat_id>": "<friendly name>"}},
# re-applied on every build AND load (hand-edits to the regenerated
# channel_directory.json don't survive); also lets a chat be pre-named before first traffic.
CHANNEL_ALIASES_PATH: Optional[Path] = None

# Slack refresh failures recur on every timed rebuild (missing scope, revoked
# token); warn once per (team, error detail) per interval, then DEBUG.
_SLACK_DIRECTORY_WARNING_INTERVAL_SECONDS = 3600
_slack_directory_warning_last: Dict[tuple[str, str], float] = {}

# Platforms whose historical session origins must never become send targets.
_SKIP_SESSION_DISCOVERY = frozenset({"local", "api_server", "webhook"})
_SLACK_RAW_ID_PREFIXES = ("C0", "D0", "G0")


def _directory_path() -> Path:
    return DIRECTORY_PATH or get_hermes_home() / "channel_directory.json"


def _aliases_path() -> Path:
    return CHANNEL_ALIASES_PATH or get_hermes_home() / "channel_aliases.json"


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_json_dict(path: Path) -> Dict[str, Any]:
    """Read a JSON object from *path*; {} when missing, unreadable, or not a dict."""
    if not path.exists():
        return {}
    try:
        data = _read_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _apply_channel_aliases(platforms: Dict[str, Any]) -> None:
    """Overlay friendly names onto directory entries by chat_id, in place.

    An aliased id not yet discovered gets a placeholder entry so a freshly-created
    group is addressable by name before its first message.
    """
    for plat_name, id_map in _load_json_dict(_aliases_path()).items():
        if not isinstance(id_map, dict):
            continue
        entries = platforms.setdefault(plat_name, [])
        if not isinstance(entries, list):
            continue
        for chat_id, friendly in id_map.items():
            if not isinstance(friendly, str) or not friendly.strip():
                continue
            chat_id, friendly = str(chat_id), friendly.strip()
            matches = [e for e in entries if isinstance(e, dict) and e.get("id") == chat_id]
            for e in matches:
                e["name"] = friendly
            if not matches:
                entries.append({"id": chat_id, "name": friendly, "thread_id": None,
                                "type": "group" if chat_id.endswith("@g.us") else "dm"})


def _normalize_channel_query(value: str) -> str:
    return value.lstrip("#").strip().lower()


def _channel_target_name(platform_name: str, channel: Dict[str, Any]) -> str:
    """Human-facing target label for a channel entry."""
    name = channel["name"]
    if platform_name == "discord":
        return f"#{name}" if channel.get("guild") else name
    return f"{name} ({channel['type']})" if channel.get("type") else name


def _session_entry_id(origin: Dict[str, Any]) -> Optional[str]:
    chat_id = origin.get("chat_id")
    if not chat_id:
        return None
    return f"{chat_id}:{thread_id}" if (thread_id := origin.get("thread_id")) else str(chat_id)


def _session_entry_name(origin: Dict[str, Any]) -> str:
    base_name = origin.get("chat_name") or origin.get("user_name") or str(origin.get("chat_id"))
    if not (thread_id := origin.get("thread_id")):
        return base_name
    return f"{base_name} / {origin.get('chat_topic') or f'topic {thread_id}'}"


def _report_slack_failure(team_id: str, error_code: Optional[str], detail: str) -> None:
    """missing_scope is expected (session-history fallback); anything else warns once per interval."""
    if error_code == "missing_scope":
        logger.debug("Channel directory: Slack team %s lacks channels:read; using session history only", team_id)
        return
    key = (str(team_id), str(detail))
    now = time.monotonic()
    last = _slack_directory_warning_last.get(key)
    if last is None or now - last >= _SLACK_DIRECTORY_WARNING_INTERVAL_SECONDS:
        _slack_directory_warning_last[key] = now
        logger.warning("Channel directory: failed to list Slack channels for team %s: %s", team_id, detail)
    else:
        logger.debug("Channel directory: suppressed repeated Slack channel list failure for team %s: %s", team_id, detail)


# --- Build / refresh -------------------------------------------------------

async def build_channel_directory(
    adapters: Dict[Any, Any],
    profile_adapters: Optional[Dict[str, Dict[Any, Any]]] = None,
) -> Dict[str, Any]:
    """Build the directory from connected adapters + session data and persist it.

    ``profile_adapters`` (fork) is the multiplex gateway's ``self._profile_adapters`` (profile name
    -> {Platform: adapter}) under ``gateway.multiplex_profiles: true``. Session-discovered entries
    (the only kind Telegram-style platforms produce) are tagged with a ``"profile"`` key for every
    secondary profile so a DM chat_id that collides across profiles (Telegram DMs key on the *user's*
    id, identical no matter which bot they talk to) doesn't collapse into one ambiguous entry.
    Entries with no ``"profile"`` key belong to the default profile or to a platform with no profile
    concept (Discord/Slack). Each secondary profile also gets its own filtered copy in its home.
    """
    from gateway.config import Platform
    platforms: Dict[str, List[Dict[str, str]]] = {}
    for platform, adapter in adapters.items():
        try:
            list_channels = getattr(adapter, "list_channels", None)
            if callable(list_channels):
                platform_channels = await list_channels()
                if platform_channels is not None:
                    platforms[platform.value] = _normalize_adapter_channels(platform_channels)
                    continue
            if platform == Platform.DISCORD:
                platforms["discord"] = await asyncio.to_thread(_build_discord, adapter)
            elif platform == Platform.SLACK:
                platforms["slack"] = await _build_slack(adapter)
        except Exception as e:
            logger.warning("Channel directory: failed to build %s: %s", platform.value, e)
    # Platforms without channel enumeration get session-based discovery, but only when
    # connected in THIS gateway process: origins for disabled or decommissioned
    # platforms must not resurface as stale send targets.
    adapter_platform_names = {getattr(p, "value", str(p)) for p in adapters}
    async def _discover(plat_name: str) -> None:
        if plat_name in _SKIP_SESSION_DISCOVERY or plat_name in platforms or plat_name not in adapter_platform_names:
            return
        platforms[plat_name] = await asyncio.to_thread(_build_from_sessions, plat_name)
    for plat in Platform:
        await _discover(plat.value)
    # Plugin platforms are dynamic enum members missing from Platform.__members__.
    with contextlib.suppress(Exception):
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            await _discover(entry.name)
    # Secondary multiplex profiles (fork): each has its own HERMES_HOME (and thus its own
    # state.db / sessions.json), invisible to the default-profile-only scan above. Adapter-enumerable
    # platforms (Discord/Slack) aren't handled here: a secondary profile enabling one of those is
    # already rejected at startup (it can't bind its own port), so session-based discovery covers
    # everything multiplex actually supports.
    profile_homes: Dict[str, Any] = {}
    if profile_adapters:
        try:
            from hermes_cli.profiles import get_profile_dir
        except Exception:
            get_profile_dir = None
        for profile_name, plat_adapters in profile_adapters.items():
            try:
                profile_home = get_profile_dir(profile_name) if get_profile_dir else None
            except Exception:
                profile_home = None
            profile_homes[profile_name] = profile_home
            for platform in plat_adapters.keys():
                plat_name = platform.value if hasattr(platform, "value") else str(platform)
                if plat_name in _SKIP_SESSION_DISCOVERY:
                    continue
                entries = await asyncio.to_thread(_build_from_sessions, plat_name, home_override=profile_home)
                for entry in entries:
                    entry["profile"] = profile_name
                platforms.setdefault(plat_name, []).extend(entries)
    _apply_channel_aliases(platforms)
    directory = {"updated_at": datetime.now().isoformat(), "platforms": platforms}
    try:
        await asyncio.to_thread(atomic_json_write, _directory_path(), directory)
    except Exception as e:
        logger.warning("Channel directory: failed to write: %s", e)
    # Each secondary profile also gets its OWN copy, filtered to its targets (its tagged entries plus
    # the untagged default-profile ones: the view resolve_channel_name(profile=...) serves in-process).
    # DIRECTORY_PATH resolves lazily from the current HERMES_HOME, so a process running under a
    # secondary profile's home (``hermes --profile <name> send --list``, or any reader without the
    # gateway's launch-home pin) would otherwise find no directory at all.
    for profile_name, profile_home in profile_homes.items():
        if profile_home is None:
            continue
        own_copy = {
            "updated_at": directory["updated_at"],
            "platforms": {plat_name: _filter_by_profile(channels, profile_name)
                          for plat_name, channels in platforms.items()},
        }
        try:
            await asyncio.to_thread(
                atomic_json_write, Path(profile_home) / "channel_directory.json", own_copy)
        except Exception as e:
            logger.warning("Channel directory: failed to write profile '%s' copy: %s", profile_name, e)
    return directory


def _build_discord(adapter) -> List[Dict[str, str]]:
    """Enumerate text + forum channels the Discord bot can see, plus session DMs."""
    channels = []
    client = getattr(adapter, "_client", None)
    if not client:
        return channels
    try:
        import discord as _discord  # noqa: F401 — SDK presence check
    except ImportError:
        return channels
    for guild in client.guilds:
        # Forum channels (type 15): creating a message auto-spawns a thread post.
        forums = getattr(guild, "forum_channels", None) or []
        for chs, ch_type in ((guild.text_channels, "channel"), (forums, "forum")):
            for ch in chs:
                channels.append({"id": str(ch.id), "name": ch.name, "guild": guild.name, "type": ch_type})
    # DM-capable users aren't reachable via guild enumeration; they come from sessions.
    channels.extend(_build_from_sessions("discord"))
    return channels


def _slack_api_error_code(error: Exception) -> Optional[str]:
    """Slack Web API error code from SlackApiError-like exceptions."""
    with contextlib.suppress(Exception):
        value = error.response.get("error")
        return str(value) if value else None
    return None


def _normalize_adapter_channels(raw_channels: Any) -> List[Dict[str, Any]]:
    """Validate and dedupe entries returned by an adapter's ``list_channels()`` hook."""
    channels: List[Dict[str, Any]] = []
    seen_ids = set()
    for raw in raw_channels if isinstance(raw_channels, list) else ():
        if not isinstance(raw, dict):
            continue
        channel_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or channel_id).strip()
        if not channel_id or not name or channel_id in seen_ids:
            continue
        entry: Dict[str, Any] = {"id": channel_id, "name": name, "type": str(raw.get("type") or "dm")}
        entry.update({key: str(raw[key]) for key in ("thread_id", "guild") if raw.get(key)})
        channels.append(entry)
        seen_ids.add(channel_id)
    return channels


def _slack_base_id(entry_id: str) -> str:
    """Thread-qualified IDs (``C0xxx:ts``) are internal routing keys, not Slack API IDs."""
    return entry_id.split(":", 1)[0]


def _slack_has_raw_name(entry: Dict[str, Any]) -> bool:
    return entry.get("name", "").startswith(_SLACK_RAW_ID_PREFIXES)


async def _slack_team_channels(team_id: str, client, seen_ids: set) -> List[Dict[str, Any]]:
    """``users.conversations`` for one workspace (public + private member channels), paginated."""
    channels: List[Dict[str, Any]] = []
    try:
        cursor: Optional[str] = None
        for _page in range(20):  # safety cap on pagination
            response = await client.users_conversations(
                types="public_channel,private_channel", exclude_archived=True, limit=200, cursor=cursor,
            )
            if not response.get("ok"):
                error_code = response.get("error", "unknown")
                _report_slack_failure(team_id, error_code, f"users.conversations not ok: {error_code}")
                break
            for ch in response.get("channels", []):
                cid, name = ch.get("id"), ch.get("name")
                if not cid or not name or cid in seen_ids:
                    continue
                seen_ids.add(cid)
                channels.append({"id": cid, "name": name, "type": "private" if ch.get("is_private") else "channel"})
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        _report_slack_failure(team_id, _slack_api_error_code(e), str(e))
    return channels


async def _slack_resolve_raw_names(client, channels: List[Dict[str, Any]]) -> None:
    """Name remaining raw-ID entries (DMs, channels outside bot scope) via
    conversations.info + users.info once per base conversation, concurrently."""
    unresolved_by_base: Dict[str, list] = {}
    for entry in channels:
        if _slack_has_raw_name(entry):
            unresolved_by_base.setdefault(_slack_base_id(entry["id"]), []).append(entry)
    if not unresolved_by_base:
        return
    async def _resolve_base(base_id: str, entries: list) -> None:
        try:
            resp = await client.conversations_info(channel=base_id)
            if not resp.get("ok"):
                return
            ch_info = resp.get("channel", {})
            resolved_name = resolved_type = None
            if not ch_info.get("is_im"):
                resolved_name = ch_info.get("name") or ch_info.get("name_normalized")
            elif ch_info.get("user", ""):
                user_resp = await client.users_info(user=ch_info["user"])
                if user_resp.get("ok"):
                    u = user_resp["user"]
                    resolved_name = u.get("profile", {}).get("display_name") or u.get("real_name") or u.get("name")
                    resolved_type = "dm"
            for entry in entries if resolved_name else ():
                entry["name"] = resolved_name
                if resolved_type:
                    entry["type"] = resolved_type
        except Exception as e:
            logger.debug("Channel directory: failed to resolve %s: %s", base_id, e)
    await asyncio.gather(*[_resolve_base(bid, ents) for bid, ents in unresolved_by_base.items()])


async def _build_slack(adapter) -> List[Dict[str, Any]]:
    """List Slack channels the bot has joined across all workspaces, merged with
    session-history DMs. Missing channels:read falls back to session history quietly."""
    team_clients = getattr(adapter, "_team_clients", None) or {}
    if not team_clients:
        return await asyncio.to_thread(_build_from_sessions, "slack")
    channels: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for team_id, client in team_clients.items():
        channels.extend(await _slack_team_channels(team_id, client, seen_ids))
    # Merge session-history DM/group entries, naming raw-ID entries from the
    # API-discovered channels where the base conversation ID is known.
    api_name_lookup = {ch["id"]: ch["name"] for ch in channels}
    for entry in await asyncio.to_thread(_build_from_sessions, "slack"):
        eid = entry.get("id")
        if not isinstance(eid, str) or eid in seen_ids:
            continue
        if _slack_has_raw_name(entry) and _slack_base_id(eid) in api_name_lookup:
            entry["name"] = api_name_lookup[_slack_base_id(eid)]
        channels.append(entry)
        seen_ids.add(eid)
    await _slack_resolve_raw_names(next(iter(team_clients.values())), channels)
    return channels


def _build_from_sessions(platform_name: str, home_override: Optional[Any] = None) -> List[Dict[str, str]]:
    """Known channels/contacts from session origins: state.db first, sessions.json fallback (pre-migration).

    state.db is the primary source (#9006): gateway session rows persist origin_json.
    ``home_override`` (fork) reads a secondary multiplex profile's own HERMES_HOME instead of the
    default profile's.
    """
    return (_build_from_sessions_db(platform_name, home_override=home_override)
            or _build_from_sessions_json(platform_name, home_override=home_override))


def _entries_from_origins(platform_name: str, source: str, origins_fn) -> List[Dict[str, Any]]:
    """Deduped entries for the (origin, chat_type) pairs from ``origins_fn()``; a mid-iteration
    failure keeps entries read so far."""
    entries: List[Dict[str, Any]] = []
    try:
        seen_ids = set()
        for origin, chat_type in origins_fn():
            entry_id = _session_entry_id(origin)
            if not entry_id or entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entries.append({
                "id": entry_id, "name": _session_entry_name(origin),
                "type": chat_type, "thread_id": origin.get("thread_id"),
            })
    except Exception as e:
        logger.debug("Channel directory: %s for %s: %s", source, platform_name, e)
    return entries


def _build_from_sessions_db(platform_name: str, home_override: Optional[Any] = None) -> List[Dict[str, str]]:
    """Pull channels/contacts from state.db gateway session rows.

    ``home_override`` (fork): read-only attach to a secondary multiplex profile's own state.db, which
    never contends with the owning profile's gateway writer and skips schema init on a DB we don't own.
    """
    def _origins() -> Iterable[Tuple[Dict[str, Any], Any]]:
        from hermes_state_registry import acquire, release_or_close
        if home_override is not None:
            from hermes_state import SessionDB
            db_path = Path(home_override) / "state.db"
            if not db_path.exists():
                return
            db = SessionDB(db_path=db_path, read_only=True)
        else:
            db = acquire()
        try:
            lister = getattr(db, "list_gateway_sessions", None)
            if not callable(lister):
                return
            rows = lister(platform=platform_name, active_only=False)
        finally:
            release_or_close(db)
        for row in rows:
            origin = None
            with contextlib.suppress(TypeError, ValueError):
                origin = json.loads(row["origin_json"]) if row.get("origin_json") else None
            if not isinstance(origin, dict) or not origin:
                origin = {"chat_id": row.get("chat_id"), "thread_id": row.get("thread_id"), "chat_name": row.get("display_name")}
            yield origin, row.get("chat_type") or "dm"
    return _entries_from_origins(platform_name, "state.db session read failed", _origins)


def _build_from_sessions_json(platform_name: str, home_override: Optional[Any] = None) -> List[Dict[str, str]]:
    """Legacy fallback: pull channels/contacts from sessions.json origin data (``home_override``: fork)."""
    home = Path(home_override) if home_override is not None else get_hermes_home()
    sessions_path = home / "sessions" / "sessions.json"
    if not sessions_path.exists():
        return []
    def _origins() -> Iterable[Tuple[Dict[str, Any], Any]]:
        for _key, session in _read_json(sessions_path).items():
            # Keys starting with "_" (e.g. the gateway's "_README") are metadata sentinels.
            if str(_key).startswith("_") or not isinstance(session, dict):
                continue
            origin = session.get("origin") or {}
            if origin.get("platform") == platform_name:
                yield origin, session.get("chat_type", "dm")
    return _entries_from_origins(platform_name, "failed to read sessions", _origins)


# --- Read / resolve --------------------------------------------------------

def load_directory() -> Dict[str, Any]:
    """Load the cached directory from disk, with aliases re-applied on read."""
    directory_path = _directory_path()
    if directory_path.exists():
        with contextlib.suppress(Exception):
            data = _read_json(directory_path)
            # Aliases apply on read too, so new names take effect between timed rebuilds.
            _apply_channel_aliases(data.setdefault("platforms", {}))
            return data
    base = {"updated_at": None, "platforms": {}}
    _apply_channel_aliases(base["platforms"])
    return base


def lookup_channel_type(platform_name: str, chat_id: str) -> Optional[str]:
    """Channel ``type`` string (e.g. ``"channel"``, ``"forum"``) for *chat_id*, or None if unknown."""
    channels = load_directory().get("platforms", {}).get(platform_name, [])
    return next((ch.get("type") for ch in channels if ch.get("id") == chat_id), None)


def _filter_by_profile(channels: List[Dict[str, Any]], profile: Optional[str]) -> List[Dict[str, Any]]:
    """Scope a channel list to one multiplex profile's own targets (fork).

    An entry with no ``"profile"`` key belongs to the default profile (or to a platform with no
    per-profile concept, like Discord/Slack); those never get tagged, see ``build_channel_directory``.
    A falsy *profile* (non-multiplex gateways, or no session-profile context) returns every entry
    unfiltered, identical to pre-multiplex behavior.
    """
    if not profile:
        return channels
    return [ch for ch in channels if ch.get("profile") in (None, profile)]


def _dedupe_channels(channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse entries that name the same target id, keeping the first (fork).

    Under multiplexing one DM shows up once per profile that talks to the user (Telegram keys DMs on
    the *user's* id): untagged for the default profile plus one ``profile``-tagged copy per
    secondary. The copies are interchangeable as send targets, so one line per id is the honest
    listing. Entries without an ``id`` are kept as they are.
    """
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for ch in channels:
        key = ch.get("id") if isinstance(ch, dict) else None
        if key is None:
            unique.append(ch)
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(ch)
    return unique


def directory_view_for_profile(
    platforms: Optional[Dict[str, Any]], profile: Optional[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """``platforms`` scoped to *profile* with duplicate targets collapsed (fork).

    The view ``hermes send --list`` prints and ``--json`` emits: other multiplex profiles' tagged
    entries are dropped (``_filter_by_profile``; a falsy *profile* keeps everything) and each
    remaining target id is listed once (``_dedupe_channels``).
    """
    return {
        plat_name: _dedupe_channels(_filter_by_profile(list(channels or []), profile))
        for plat_name, channels in (platforms or {}).items()
    }


def resolve_channel_name(platform_name: str, name: str, profile: Optional[str] = None) -> Optional[str]:
    """Resolve a friendly channel name (e.g. "bot-home", "#bot-home", "GuildName/bot-home",
    Slack "#engineering") to an ID; case-insensitive, first match wins.

    ``profile`` (fork) scopes the search to one multiplex profile's own targets: a Telegram DM's
    chat_id is the *user's* id and therefore identical across every bot the user talks to, so a
    secondary profile could otherwise resolve a name to another profile's entry and send through
    the wrong bot.
    """
    channels = load_directory().get("platforms", {}).get(platform_name, [])
    channels = _filter_by_profile(channels, profile)
    if not channels:
        return None
    # 0. Exact ID match — case-sensitive, no normalization, so raw platform IDs (e.g. Slack
    # "C0B0QV5434G") work even when _parse_target_ref's format guard didn't recognize them.
    raw = name.strip()
    for ch in channels:
        if ch.get("id") == raw:
            return ch["id"]
    query = _normalize_channel_query(name)
    # 1. Exact name match, including the display labels shown by send_message(action="list")
    for ch in channels:
        if query in (_normalize_channel_query(ch["name"]), _normalize_channel_query(_channel_target_name(platform_name, ch))):
            return ch["id"]
    # 2. Guild-qualified match for Discord ("GuildName/channel")
    if "/" in query:
        guild_part, ch_part = query.rsplit("/", 1)
        for ch in channels:
            guild = ch.get("guild", "").strip().lower()
            if guild == guild_part and _normalize_channel_query(ch["name"]) == ch_part:
                return ch["id"]
    # 3. Partial prefix match (only if unambiguous)
    matches = [ch for ch in channels if _normalize_channel_query(ch["name"]).startswith(query)]
    return matches[0]["id"] if len(matches) == 1 else None


def format_directory_for_display(
    platforms: Optional[Dict[str, Any]] = None, profile: Optional[str] = None,
) -> str:
    """Format the channel directory as a human-readable list for the model.

    ``platforms`` overrides the on-disk directory (``hermes send --list`` merges in
    configured-but-undiscovered platforms); an empty channel list renders a "(no channels
    discovered yet)" hint because the platform is still a valid send target.
    ``profile`` (fork) scopes the listing to one multiplex profile's own targets, see
    ``_filter_by_profile``.
    """
    if platforms is None:
        platforms = load_directory().get("platforms", {})
    if profile is not None:
        platforms = {plat_name: _filter_by_profile(channels, profile) for plat_name, channels in platforms.items()}
    # One line per target (fork): the multiplex directory carries one copy of a shared DM per profile
    # (see _dedupe_channels), which is directory bookkeeping, not six different places to send to.
    platforms = {plat_name: _dedupe_channels(list(channels or [])) for plat_name, channels in platforms.items()}
    if not platforms:
        return "No messaging platforms connected or no channels discovered yet."
    lines = ["Available messaging targets:\n"]
    for plat_name, channels in sorted(platforms.items()):
        if not channels:
            lines.append(f"{plat_name.title()}:")
            lines.append(
                f"  (no channels discovered yet — send directly with "
                f"{plat_name}:<chat_id>, or bare '{plat_name}' for the home channel)"
            )
        elif plat_name == "discord":
            # Group Discord channels by guild (sorted by name); DMs last, in discovery order.
            guilds: Dict[str, List] = {}
            dms: List = []
            for ch in channels:
                (guilds.setdefault(ch["guild"], []) if ch.get("guild") else dms).append(ch)
            groups = [(f"Discord ({g}):", sorted(chs, key=lambda c: c["name"])) for g, chs in sorted(guilds.items())]
            if dms:
                groups.append(("Discord (DMs):", dms))
            for header, group in groups:
                lines.append(header)
                lines.extend(f"  discord:{_channel_target_name(plat_name, ch)}" for ch in group)
        else:
            lines.append(f"{plat_name.title()}:")
            lines.extend(f"  {plat_name}:{_channel_target_name(plat_name, ch)}" for ch in channels)
        lines.append("")
    lines.append('Use these as the "target" parameter when sending.')
    lines.append('Bare platform name (e.g. "telegram") sends to home channel.')
    return "\n".join(lines)
