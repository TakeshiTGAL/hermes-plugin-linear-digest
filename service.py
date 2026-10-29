"""Morning list of open Linear issues assigned to the key's user."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

if __package__:
    from . import client, gates
    from .public_cron import CronUnread, Store, resolved_hermes_home
else:
    import client
    import gates
    from public_cron import CronUnread, Store, resolved_hermes_home

TOOLSET = "linear_digest"
JOB_NAME = "linear-digest"
SCRIPT_NAME = "linear-digest.py"
SCHEDULE = "0 8 * * *"
SETTINGS_NAME = "settings.json"
SHOW_LIMIT = 30
MAX_PAGES = 10
DEFAULT_STALE_DAYS = 7
STALE_MIN = 1
STALE_MAX = 90
TITLE_CAP = 180
CLOSED_TYPES = frozenset({"completed", "canceled", "duplicate"})
SELF_TARGETS = frozenset({"origin", "local", "all"})

SILENT_LINE = "The script stdout is the message. An empty script stdout is [SILENT]."

WATCH_SCRIPT = '''"""linear-digest morning list. Hermes delivers stdout and sends nothing when it is empty."""
import os
import sys
import time


def _adopt_hermes_zone():
    """Use the Hermes clock for this process before the list is built."""
    name = ""
    try:
        import hermes_time
        hermes_time.reset_cache()
        name = hermes_time.get_timezone_name() or ""
    except Exception:
        name = os.environ.get("HERMES_TIMEZONE", "").strip()
    if name:
        os.environ["TZ"] = name
        if hasattr(time, "tzset"):
            time.tzset()


_adopt_hermes_zone()

from hermes_constants import get_hermes_home

plugin = get_hermes_home() / "plugins" / "linear-digest"
if not (plugin / "service.py").is_file():
    print(
        "linear-digest is not installed under plugins/linear-digest, so the list did not run.",
        file=sys.stderr,
    )
    raise SystemExit(1)
sys.path.insert(0, str(plugin))
import service

text, error = service.digest_stdout()
if error:
    print(error, file=sys.stderr)
    raise SystemExit(1)
if text:
    sys.stdout.write(text)
    if not text.endswith("\\n"):
        sys.stdout.write("\\n")
'''


class Deps:
    def __init__(self) -> None:
        self.transport: client.Transport | None = None
        self.data_dir: Path | None = None
        self.today: date | None = None
        self.now: datetime | None = None
        self.cron: Any | None = None
        self.secrets: Callable[[str], tuple[str | None, str | None]] | None = None
        self.idle_seconds: float = client.IDLE_SECONDS
        self.total_seconds: float = client.TOTAL_SECONDS
        self.body_cap: int = client.BODY_CAP
        self.show_limit: int = SHOW_LIMIT
        self.max_pages: int = MAX_PAGES


def digest_stdout(deps: Deps | None = None) -> tuple[str, str | None]:
    """Text for stdout, and an error line when the list was not read.

    An empty string and no error means there is nothing to send.
    """
    if deps is None:
        deps = Deps()
    try:
        return _digest(deps)
    except client.ClientError as exc:
        return "", exc.message
    except Exception:
        return "", "The Linear request failed. Nothing was fetched."


def linear_digest(deps: Deps, args: dict | None) -> str:
    """Same text as the morning script. Empty when there is nothing to say."""
    if args:
        return "Unknown arguments were rejected. Nothing was fetched."
    text, error = digest_stdout(deps)
    if error:
        return error
    return text


def _digest(deps: Deps) -> tuple[str, str | None]:
    stale, problem = _stale_days(deps)
    if problem:
        return "", problem
    issues, complete = _pages(deps)
    text = _render(issues, complete, deps)
    return text, None


def _secret(deps: Deps, name: str) -> tuple[str | None, str | None]:
    reader = deps.secrets or client.read_secret
    return reader(name)


def _pages(deps: Deps) -> tuple[list[dict[str, Any]], bool]:
    seen: set[str] = set()
    cursor: str | None = None
    found: list[dict[str, Any]] = []
    complete = False
    for _page in range(deps.max_pages):
        key, key_error = _secret(deps, "LINEAR_API_KEY")
        if key_error:
            raise client.ClientError(key_error)
        if key is None or key.strip() == "":
            raise client.ClientError("missing_key")
        header_key = key.strip()
        if not client.header_safe(header_key):
            raise client.ClientError("bad_key")
        rehearsal, rehearsal_error = _secret(deps, "LINEAR_DIGEST_REHEARSAL")
        if rehearsal_error:
            raise client.ClientError(rehearsal_error)
        url = client.endpoint(rehearsal)
        data = client.post_assigned(
            url,
            header_key,
            {"after": cursor},
            idle=deps.idle_seconds,
            total=deps.total_seconds,
            cap=deps.body_cap,
            transport=deps.transport,
        )
        nodes, has_next, end_cursor = _page_nodes(data)
        for node in nodes:
            found.append(_issue(node))
        if not has_next:
            complete = True
            break
        if not isinstance(end_cursor, str) or not end_cursor.strip() or end_cursor in seen:
            break
        seen.add(end_cursor)
        cursor = end_cursor
    return found, complete


def _page_nodes(data: dict[str, Any]) -> tuple[list[Any], bool, Any]:
    viewer = data.get("viewer")
    if not isinstance(viewer, dict):
        raise client.ClientError("bad_body")
    connection = viewer.get("assignedIssues")
    if not isinstance(connection, dict):
        raise client.ClientError("bad_body")
    nodes = connection.get("nodes")
    info = connection.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(info, dict):
        raise client.ClientError("bad_body")
    has_next = info.get("hasNextPage")
    if not isinstance(has_next, bool):
        raise client.ClientError("bad_body")
    return nodes, has_next, info.get("endCursor")


def _issue(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise client.ClientError("bad_body")
    identifier = node.get("identifier")
    title = node.get("title")
    updated = node.get("updatedAt")
    url = node.get("url")
    if not all(isinstance(value, str) for value in (identifier, title, updated, url)):
        raise client.ClientError("bad_body")
    identifier_text = _one_line(identifier, 40)
    if not identifier_text:
        raise client.ClientError("bad_body")
    state_name, state_type = _state(node.get("state"))
    due, due_label = _due(node.get("dueDate"))
    return {
        "identifier": identifier_text,
        "title": _one_line(title, TITLE_CAP) or "(no title)",
        "state": _one_line(state_name, 80) or "unknown",
        "state_type": state_type,
        "due": due,
        "due_label": due_label,
        "updated": _parse_time(updated),
        "url": _issue_url(url),
    }


def _state(value: Any) -> tuple[str, str]:
    if value is None:
        return "unknown", ""
    if not isinstance(value, dict):
        raise client.ClientError("bad_body")
    name = value.get("name")
    kind = value.get("type")
    if not isinstance(name, str) or not isinstance(kind, str):
        raise client.ClientError("bad_body")
    return name, kind


def _due(value: Any) -> tuple[date | None, str]:
    if value is None:
        return None, "due none"
    if not isinstance(value, str) or len(value) != 10:
        return None, "due unreadable"
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None, "due unreadable"
    if parsed.isoformat() != value:
        return None, "due unreadable"
    return parsed, f"due {value}"


def _parse_time(value: str) -> datetime | None:
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _issue_url(value: str) -> str:
    if len(value) > 500 or any(ord(char) < 33 or ord(char) == 127 for char in value):
        return "url refused"
    parts = urlsplit(value)
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        return "url refused"
    if host == "linear.app" or host.endswith(".linear.app"):
        return value
    return "url refused"


def _one_line(value: str, limit: int) -> str:
    chars: list[str] = []
    for char in value:
        if ord(char) < 32 or ord(char) == 127 or char in {"\u2028", "\u2029"}:
            chars.append(" ")
            continue
        chars.append(char)
    text = " ".join("".join(chars).split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _config_timezone_name() -> str:
    """Top-level ``timezone`` when ``hermes_time`` cannot be imported."""
    try:
        from hermes_constants import get_config_path
        path = get_config_path()
    except Exception:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for raw in text.splitlines():
        if not raw or raw[0] in " \t#":
            continue
        if not raw.startswith("timezone:"):
            continue
        value = raw.split(":", 1)[1].strip().strip("\"'")
        return value
    return ""


def _fallback_zone() -> ZoneInfo | None:
    """Same order as Hermes when the public clock cannot be imported."""
    name = os.environ.get("HERMES_TIMEZONE", "").strip() or _config_timezone_name()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _hermes_zone() -> ZoneInfo | None:
    """Zone of the clock cron uses for 08:00. None means the machine's local zone."""
    try:
        import hermes_time
        hermes_time.reset_cache()
        return hermes_time.get_timezone()
    except Exception:
        return _fallback_zone()


def _instant(deps: Deps) -> datetime:
    """Aware instant. An injected ``deps.now`` is that instant. Otherwise the Hermes clock."""
    if deps.now is not None:
        value = deps.now
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        import hermes_time
        hermes_time.reset_cache()
        return hermes_time.now()
    except Exception:
        zone = _fallback_zone()
        if zone is not None:
            return datetime.now(zone)
        return datetime.now().astimezone()


def _today(deps: Deps) -> date:
    if deps.today is not None:
        return deps.today
    instant = _instant(deps)
    zone = _hermes_zone()
    if zone is None:
        return instant.astimezone().date()
    return instant.astimezone(zone).date()


def _now(deps: Deps) -> datetime:
    instant = _instant(deps)
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def _render(issues: list[dict[str, Any]], complete: bool, deps: Deps) -> str:
    open_issues = [item for item in issues if item["state_type"] not in CLOSED_TYPES]
    today = _today(deps)
    stale_days, _problem = _stale_days(deps)
    if stale_days is None:
        stale_days = DEFAULT_STALE_DAYS
    cutoff = _now(deps) - timedelta(days=stale_days)
    groups: list[list[dict[str, Any]]] = [[], [], [], []]
    for item in open_issues:
        groups[_bucket(item, today, cutoff)].append(item)
    groups[0].sort(key=lambda item: (item["due"].isoformat(), item["identifier"]))
    groups[1].sort(key=lambda item: item["identifier"])
    groups[2].sort(key=lambda item: (item["updated"].isoformat() if item["updated"] else "", item["identifier"]))
    groups[3].sort(key=lambda item: item["identifier"])
    quiet = "day" if stale_days == 1 else "days"
    labels = (
        "Overdue",
        "Due today",
        f"Not updated for {stale_days} {quiet}",
        "Other",
    )
    sections = [(f"{label} ({len(group)})", group) for label, group in zip(labels, groups) if group]
    total = sum(len(group) for _heading, group in sections)
    if complete and total == 0:
        return ""
    lines: list[str] = []
    used = 0
    for heading, group in sections:
        if used >= deps.show_limit:
            break
        take = group[: deps.show_limit - used]
        if not take:
            break
        if lines:
            lines.append("")
        lines.append(heading)
        lines.extend(_line(item) for item in take)
        used += len(take)
    hidden = total - used
    if hidden or not complete:
        if lines:
            lines.append("")
        if hidden:
            lines.append(f"+{hidden} more")
        if not complete:
            lines.append("Later pages were not read, so this is not the full list.")
    return "\n".join(lines)


def _bucket(item: dict[str, Any], today: date, cutoff: datetime) -> int:
    due = item["due"]
    if isinstance(due, date) and due < today:
        return 0
    if isinstance(due, date) and due == today:
        return 1
    updated = item["updated"]
    if isinstance(updated, datetime) and updated < cutoff:
        return 2
    return 3


def _line(item: dict[str, Any]) -> str:
    due = f"due {item['due'].isoformat()}" if isinstance(item["due"], date) else item["due_label"]
    return "\t".join((item["identifier"], item["title"], item["state"], due, item["url"]))


def _settings_path(deps: Deps) -> Path:
    if deps.data_dir is not None:
        directory = deps.data_dir
    else:
        directory = gates.plugin_data_dir()
    if not isinstance(directory, Path):
        directory = Path(directory)
    return directory / SETTINGS_NAME


def _stale_days(deps: Deps) -> tuple[int | None, str | None]:
    try:
        path = _settings_path(deps)
    except Exception:
        return None, "The settings folder could not be opened, so nothing was fetched."
    if not path.is_file():
        return DEFAULT_STALE_DAYS, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, "The settings file could not be read, so nothing was fetched."
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "The settings file could not be read, so nothing was fetched."
    if not isinstance(data, dict):
        return None, "The settings file could not be read, so nothing was fetched."
    stale = data.get("stale_days", DEFAULT_STALE_DAYS)
    if isinstance(stale, bool) or not isinstance(stale, int) or not (STALE_MIN <= stale <= STALE_MAX):
        return None, "The settings file could not be read, so nothing was fetched."
    return stale, None


def parse_stale_days(value: Any) -> int | str:
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return "Stale days must be a whole number from 1 to 90. Nothing was changed."
        value = int(text)
    if isinstance(value, bool) or not isinstance(value, int):
        return "Stale days must be a whole number from 1 to 90. Nothing was changed."
    if value < STALE_MIN or value > STALE_MAX:
        return "Stale days must be a whole number from 1 to 90. Nothing was changed."
    return value


def watch_script_path() -> Path | None:
    try:
        home = resolved_hermes_home()
    except (OSError, RuntimeError, ImportError):
        return None
    if not home:
        return None
    return Path(home) / "scripts" / SCRIPT_NAME


def write_watch_script() -> str | None:
    path = watch_script_path()
    if path is None:
        return "The Hermes home could not be resolved, so the script was not written."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "The script could not be written, so the job was not changed."
    denied = gates.write_guard_error(str(path))
    if denied:
        return denied
    payload = WATCH_SCRIPT.encode("utf-8")
    fd = None
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".digest-", suffix=".tmp")
        os.write(fd, payload)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        tmp = ""
    except OSError:
        return "The script could not be written, so the job was not changed."
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return None


def remove_watch_script() -> str | None:
    path = watch_script_path()
    if path is None:
        return "The Hermes home could not be resolved, so the script was not removed."
    if not path.is_file():
        return None
    denied = gates.write_guard_error(str(path))
    if denied:
        return denied
    try:
        path.unlink()
    except OSError:
        return "The script could not be removed."
    return None


def _save_settings(deps: Deps, data: dict[str, Any]) -> str | None:
    try:
        path = _settings_path(deps)
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return "The settings folder could not be prepared, so the file was not saved."
    denied = gates.write_guard_error(str(path))
    if denied:
        return denied
    payload = json.dumps(data, ensure_ascii=True, indent=2).encode("utf-8")
    fd = None
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
        os.write(fd, payload)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        tmp = ""
    except OSError:
        return "The settings file could not be replaced, so the saved file was left as it was."
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return None


def _load_settings(deps: Deps) -> tuple[dict[str, Any] | None, str | None]:
    try:
        path = _settings_path(deps)
    except Exception:
        return None, "The settings folder could not be opened."
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None, "The settings file could not be read, so nothing was changed."
    if not isinstance(data, dict):
        return None, "The settings file could not be read, so nothing was changed."
    return data, None


def _cron(deps: Deps) -> Any:
    if deps.cron is not None:
        return deps.cron
    return Store()


def _named_ids(mod: Any) -> tuple[list[str] | None, str | None]:
    try:
        jobs = mod.load_jobs()
    except CronUnread as exc:
        return None, exc.detail or "hermes cron list failed, so nothing was changed."
    except Exception:
        return None, "hermes cron list failed, so nothing was changed."
    if not isinstance(jobs, list):
        return None, "hermes cron list failed, so nothing was changed."
    found = []
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == JOB_NAME and job.get("id"):
            found.append(str(job["id"]))
    return found, None


def _job_exists(mod: Any, job_id: str) -> tuple[bool | None, str | None]:
    try:
        job = mod.get_job(job_id)
    except CronUnread as exc:
        return None, exc.detail or "hermes cron list failed."
    except Exception:
        return None, "hermes cron list failed."
    return isinstance(job, dict), None


def _deliver_note(canon: str) -> str:
    notes = ""
    platforms = []
    for part in canon.split(","):
        head = part.split(":", 1)[0]
        if head in SELF_TARGETS or head == "bot-chat":
            continue
        platforms.append(head)
    if platforms:
        notes += (
            f" The name {platforms[0]} is a delivery target, not proof that this machine has it set up."
        )
    if any(part == "bot-chat" or part.startswith("bot-chat:") for part in canon.split(",")):
        notes += " bot-chat puts the list into another model turn. That turn is not model-free."
    return notes


def _create_command(deliver: str) -> str:
    """The ``hermes cron create`` argv this plugin actually runs, as one pasteable line."""
    profile = gates.active_profile_name()
    prefix = "hermes" if profile == "default" else f"hermes -p {profile}"
    return (
        f'{prefix} cron create "{SCHEDULE}" "{SILENT_LINE}" --name {JOB_NAME} '
        f"--deliver {deliver} --no-agent --script {SCRIPT_NAME}"
    )


def _help(job_id: str | None, deliver: str | None = None) -> str:
    create_line = _create_command(deliver) if deliver else None
    lines = gates.hermes_cron_lines(job_id=job_id, create_line=create_line)
    return "Hermes commands for this job, for you to run:\n" + "\n".join(lines)


def schedule(deps: Deps, deliver: str, stale_text: Any) -> tuple[int, str]:
    canon = gates.canonical_deliver(deliver)
    if not canon:
        return 1, "That deliver target is not accepted. Nothing was changed."
    mod = _cron(deps)
    saved, load_error = _load_settings(deps)
    if load_error:
        return 1, load_error
    saved = saved or {}
    if stale_text is None:
        raw_stale = saved.get("stale_days", DEFAULT_STALE_DAYS)
    else:
        raw_stale = stale_text
    stale = parse_stale_days(raw_stale)
    if isinstance(stale, str):
        return 1, stale
    saved_id = str(saved.get("job_id") or "").strip()
    named, list_error = _named_ids(mod)
    if list_error:
        return 1, list_error
    named = named or []
    exists: bool | None = False
    if saved_id:
        exists, exist_error = _job_exists(mod, saved_id)
        if exist_error:
            return 1, exist_error
    if len(named) > 1:
        return 1, "More than one cron job is named linear-digest (" + ", ".join(named) + "). Nothing was changed."
    if exists:
        action = "update"
        job_id = saved_id
    elif len(named) == 1:
        action = "update"
        job_id = named[0]
    else:
        action = "create"
        job_id = ""
    script_error = write_watch_script()
    if script_error:
        return 1, script_error
    if action == "update":
        update_error = _update_job(mod, job_id, canon)
        if update_error:
            return 1, update_error
    else:
        job_id, create_error = _create_job(mod, canon)
        if create_error or not job_id:
            leftover = remove_watch_script()
            base = create_error or "The cron job did not return an id."
            if leftover:
                return 1, base + " The script was left in place."
            return 1, base
    save_error = _save_settings(deps, {"stale_days": stale, "deliver": canon, "job_id": job_id})
    if save_error:
        return 1, save_error + f" The job id is {job_id}."
    if "bot-chat" in canon:
        cost = "The script does not call a model. The deliver target starts another model turn."
    else:
        cost = "Each run costs 0 tokens."
    text = (
        f"Scheduled linear-digest at 08:00 ({SCHEDULE}), deliver {canon}, "
        f"stale days {stale}. {cost} Empty output sends nothing."
        + _deliver_note(canon)
        + "\n"
        + _help(job_id, canon)
    )
    return 0, text


def _create_job(mod: Any, deliver: str) -> tuple[str | None, str | None]:
    create = getattr(mod, "create_job", None)
    if not callable(create):
        return None, "The cron job was not created because hermes cron could not be run."
    try:
        created = create(schedule=SCHEDULE, name=JOB_NAME, deliver=deliver, script=SCRIPT_NAME, no_agent=True)
    except Exception as exc:
        return None, f"The cron job could not be created. {exc}"
    job_id = created.get("id") if isinstance(created, dict) else None
    if not job_id:
        return None, "The cron job did not return an id."
    return str(job_id), None


def _update_job(mod: Any, job_id: str, deliver: str) -> str | None:
    update = getattr(mod, "update_job", None)
    if not callable(update):
        return "The existing job could not be updated."
    try:
        result = update(job_id, {"schedule": SCHEDULE, "deliver": deliver, "script": SCRIPT_NAME, "no_agent": True})
    except Exception as exc:
        return f"The existing job could not be updated. {exc}"
    if result is None:
        return "The existing job could not be updated."
    return None


def unschedule(deps: Deps) -> tuple[int, str]:
    saved, load_error = _load_settings(deps)
    if load_error:
        return 1, load_error
    saved = saved or {}
    job_id = str(saved.get("job_id") or "").strip()
    mod = _cron(deps)
    named, list_error = _named_ids(mod)
    if list_error:
        return 1, list_error
    named = named or []
    leave_script = any(item != job_id for item in named)
    already_gone = False
    if job_id:
        exists, exist_error = _job_exists(mod, job_id)
        if exist_error:
            return 1, exist_error
        if exists:
            remove = getattr(mod, "remove_job", None)
            removed = False
            if callable(remove):
                try:
                    removed = bool(remove(job_id))
                except Exception:
                    removed = False
            if not removed:
                return 1, f"The cron job {job_id} could not be removed, so the script was left in place."
        else:
            already_gone = True
    if leave_script:
        script_error = None
    else:
        script_error = remove_watch_script()
        if script_error:
            return 1, script_error
    if saved:
        stale = saved.get("stale_days", DEFAULT_STALE_DAYS)
        if isinstance(stale, bool) or not isinstance(stale, int):
            stale = DEFAULT_STALE_DAYS
        save_error = _save_settings(deps, {"stale_days": stale, "deliver": saved.get("deliver") or "local", "job_id": ""})
        if save_error:
            return 1, save_error
    if leave_script:
        kept = " The script was left because another cron job is also named linear-digest."
    else:
        kept = ""
    if not job_id:
        if leave_script:
            return 0, "No saved job id." + kept
        return 0, "No saved job id. The script is removed if it was there."
    if already_gone:
        if leave_script:
            return 0, f"The cron job {job_id} is already gone." + kept
        return 0, f"The cron job {job_id} is already gone. The script was removed if it was there."
    if leave_script:
        return 0, f"Removed cron job {job_id}." + kept + " Settings stay without that job id."
    return 0, f"Removed cron job {job_id} and the script. Settings stay without that job id."


def status(deps: Deps) -> tuple[int, str]:
    saved, load_error = _load_settings(deps)
    if load_error:
        return 1, load_error
    saved = saved or {}
    stale = saved.get("stale_days", DEFAULT_STALE_DAYS)
    job_id = str(saved.get("job_id") or "").strip()
    deliver = saved.get("deliver") or "local"
    if not job_id:
        return 0, f"No saved job. Stale days {stale}. Deliver {deliver}. This does not fetch."
    mod = _cron(deps)
    try:
        job = mod.get_job(job_id)
    except CronUnread as exc:
        return 1, (exc.detail or "hermes cron list failed.") + " This does not fetch."
    except Exception:
        return 1, "hermes cron list failed. This does not fetch."
    if not isinstance(job, dict):
        return 0, (
            f"Saved job {job_id} is gone. No cron job with that id remains. "
            f"Stale days {stale}. This does not fetch.\n" + _help(None)
        )
    live_schedule = str(job.get("schedule") or "").strip()
    live_deliver = str(job.get("deliver") or "").strip()
    parts = [f"Job {job_id} is on the cron list."]
    if live_schedule:
        parts.append(f"Schedule {live_schedule}.")
    else:
        parts.append("The list did not show a schedule.")
    if live_deliver:
        parts.append(f"Deliver {live_deliver}.")
    else:
        parts.append("The list did not show a deliver target.")
    if live_deliver and str(deliver) != live_deliver:
        parts.append(f"Saved deliver is {deliver}.")
    parts.append(f"Stale days {stale}.")
    parts.append("This does not fetch.")
    shown = live_deliver or str(deliver or "")
    return 0, " ".join(parts) + "\n" + _help(job_id, shown or None)
