"""Delivery allowlist, profile prefix, and the write gate."""
from __future__ import annotations

import importlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

PLUGIN_NAME = "linear-digest"
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
NEVER_DELIVER = frozenset({"cli", "cron", "api_server"})
DELIVER_EXACT = frozenset({"origin", "local", "all"})
# Platforms Hermes v0.21.6 delivers cron reports to. Later releases moved most
# of them onto the public platform registry, which is read as well. The private
# delivery set is not imported. A name in neither place is refused here. Hermes
# can still store it with `hermes cron edit <id> --deliver`.
BUILTIN_DELIVERY_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "whatsapp", "signal", "matrix", "mattermost",
    "dingtalk", "feishu", "wecom", "wecom_callback", "weixin", "sms", "email",
    "webhook", "bluebubbles", "qqbot", "yuanbao",
})
SCHEDULE_WORDS = frozenset({
    "every", "in", "at", "on", "daily", "hourly", "weekly", "monthly", "yearly", "annually",
    "once", "now", "today", "tonight", "tomorrow", "midnight", "noon", "minute", "minutes",
    "hour", "hours", "day", "days", "week", "weeks", "weekdays", "weekends", "weekday", "weekend",
})
WEEKDAYS = frozenset({
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})
_DURATION = re.compile(
    r"(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\Z",
    re.IGNORECASE,
)


def write_guard_error(path: str) -> str | None:
    try:
        module = importlib.import_module("agent.file_safety")
    except Exception:
        return "The write check could not be loaded, so the file was not saved."
    fn = getattr(module, "get_write_denied_error", None)
    if not callable(fn):
        return "The write check could not be loaded, so the file was not saved."
    try:
        denied = fn(path)
    except Exception:
        return "The write check raised, so the file was not saved."
    if denied:
        return str(denied)
    return None


def plugin_data_dir() -> Path:
    try:
        module = importlib.import_module("plugins.plugin_storage")
    except Exception as exc:
        raise RuntimeError("plugin_data_dir is not available") from exc
    fn = getattr(module, "plugin_data_dir", None)
    if not callable(fn):
        raise RuntimeError("plugin_data_dir is not available")
    path = fn(PLUGIN_NAME)
    if not isinstance(path, Path):
        path = Path(path)
    return path


_REMEMBERED_PROFILE: str | None = None


def remember_profile_name(reported: Any) -> None:
    """Store the public ctx.profile_name. An unusable value is forgotten."""
    global _REMEMBERED_PROFILE
    if isinstance(reported, str) and PROFILE_NAME.fullmatch(reported.strip()):
        _REMEMBERED_PROFILE = reported.strip()
        return
    _REMEMBERED_PROFILE = None


def active_profile_name() -> str:
    if _REMEMBERED_PROFILE is None:
        return "default"
    return _REMEMBERED_PROFILE


def hermes_cron_lines(
    profile: str | None = None,
    job_id: str | None = None,
    create_line: str | None = None,
) -> list[str]:
    """Lines the user can paste. run and remove need an id, so they are left out without one.

    ``create`` is included only as ``create_line``, the argv that was actually run.
    A bare ``hermes cron create`` is not a command for this job.
    """
    name = active_profile_name() if profile is None else profile
    prefix = "hermes" if name == "default" else f"hermes -p {name}"
    lines = []
    if create_line:
        lines.append(create_line)
    ident = str(job_id).strip() if job_id else ""
    for verb in ("run", "list", "status", "remove"):
        if verb in {"run", "remove"}:
            if ident:
                lines.append(f"{prefix} cron {verb} {ident}")
            continue
        lines.append(f"{prefix} cron {verb}")
    return lines


def load_platforms() -> set[str] | None:
    """Registry names that declare a cron delivery env var, or None if it cannot be read.

    The built-in table is accepted either way. An unreadable registry does not
    drop those names.
    """
    try:
        module = importlib.import_module("gateway.platform_registry")
    except Exception:
        return None
    registry = getattr(module, "platform_registry", None)
    if registry is None:
        return None
    try:
        names: set[str] = set()
        for entry_name in registry.registered_names():
            text = str(entry_name).strip().lower()
            if not text:
                continue
            entry = registry.get(text)
            if entry is not None and getattr(entry, "cron_deliver_env_var", None):
                names.add(text)
    except Exception:
        return None
    return names - NEVER_DELIVER


def _duration(text: str) -> bool:
    return _DURATION.fullmatch(text.strip()) is not None


def _looks_like_schedule(target: str) -> bool:
    word = target.strip().lower()
    if not word:
        return False
    if word in SCHEDULE_WORDS or word in WEEKDAYS or word.rstrip("s") in WEEKDAYS:
        return True
    if word[0].isdigit() or word[0] in "*@":
        return True
    if "/" in word or _duration(word):
        return True
    try:
        datetime.fromisoformat(word.replace("z", "+00:00"))
    except ValueError:
        return False
    return True


_MISSING = object()


def canonical_deliver(deliver: str, platforms: Any = _MISSING) -> str | None:
    if platforms is _MISSING:
        platforms = load_platforms()
    allowed = set(BUILTIN_DELIVERY_PLATFORMS)
    if platforms:
        allowed |= set(platforms)
    allowed -= NEVER_DELIVER
    canon: list[str] = []
    for part in str(deliver).split(","):
        piece = part.strip()
        if not piece or any(ch.isspace() for ch in piece) or _looks_like_schedule(piece):
            return None
        low = piece.lower()
        if low in NEVER_DELIVER:
            return None
        if low in DELIVER_EXACT:
            canon.append(low)
            continue
        if low == "bot-chat":
            canon.append(low)
            continue
        if low.startswith("bot-chat:"):
            chat = piece.split(":", 1)[1].strip()
            if not chat or any(ch.isspace() for ch in chat):
                return None
            canon.append(f"bot-chat:{chat}")
            continue
        platform, sep, chat = piece.partition(":")
        key = platform.lower()
        if key not in allowed:
            return None
        if not sep:
            canon.append(key)
            continue
        if not chat or any(ch.isspace() for ch in chat):
            return None
        canon.append(f"{key}:{chat}")
    return ",".join(canon) if canon else None
