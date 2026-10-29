"""Ordering, caps, secrets, and refusals. The transport never opens a socket."""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import client
import service

KEY = "lin_api_TESTKEYVALUE123456"
TODAY = date(2026, 10, 29)
NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)


def issue(identifier, title="Title", state="Todo", kind="started", due="2026-11-01", updated="2026-10-28T00:00:00.000Z", url=None):
    return {
        "identifier": identifier,
        "title": title,
        "url": url or f"https://linear.app/acme/issue/{identifier}",
        "dueDate": due,
        "updatedAt": updated,
        "state": {"name": state, "type": kind},
    }


def page(nodes, has_next=False, cursor=None):
    return {
        "data": {
            "viewer": {
                "assignedIssues": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                }
            }
        }
    }


class Transport:
    def __init__(self, pages, status=200):
        self.pages = list(pages)
        self.status = status
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        body = self.pages.pop(0) if self.pages else page([])
        if isinstance(body, bytes):
            raw = body
        else:
            raw = json.dumps(body).encode("utf-8")
        return self.status, {}, raw


def deps(tmp_path, transport, **kwargs):
    box = service.Deps()
    box.data_dir = tmp_path / "data"
    box.transport = transport
    box.today = TODAY
    box.now = NOW
    box.secrets = lambda name: secrets(name)
    for key, value in kwargs.items():
        setattr(box, key, value)
    return box


def secrets(name, key=KEY, rehearsal=None):
    if name == "LINEAR_API_KEY":
        return key, None
    if name == "LINEAR_DIGEST_REHEARSAL":
        return rehearsal, None
    return None, None


def test_order_overdue_today_quiet_then_rest(tmp_path):
    nodes = [
        issue("LIN-9", due=None, updated="2026-10-28T00:00:00.000Z"),
        issue("LIN-2", due="2026-10-29", updated="2026-01-01T00:00:00.000Z"),
        issue("LIN-1", due="2026-10-01", updated="2026-10-28T00:00:00.000Z"),
        issue("LIN-4", due="2026-10-20", updated="2026-01-01T00:00:00.000Z"),
        issue("LIN-3", due=None, updated="2026-09-01T00:00:00.000Z"),
        issue("LIN-8", due=None, updated="2026-10-20T00:00:00.000Z", kind="completed"),
    ]
    transport = Transport([page(nodes)])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert error is None
    assert KEY not in text
    rows = [line for line in text.splitlines() if "\t" in line]
    assert [line.split("\t")[0] for line in rows] == ["LIN-1", "LIN-4", "LIN-2", "LIN-3", "LIN-9"]
    assert "Overdue (2)" in text
    assert "Due today (1)" in text
    assert "Not updated for 7 days (1)" in text
    assert "Other (1)" in text
    assert text.index("Overdue (2)") < text.index("Due today (1)")
    assert text.index("Due today (1)") < text.index("Not updated for 7 days (1)")
    assert text.index("Not updated for 7 days (1)") < text.index("Other (1)")
    assert "\n\nDue today (1)\n" in text
    assert "completed" not in text
    assert rows[0].split("\t")[3] == "due 2026-10-01"
    assert rows[1].split("\t")[3] == "due 2026-10-20"
    assert rows[3].split("\t")[3] == "due none"


def test_empty_assignment_is_empty_stdout(tmp_path):
    transport = Transport([page([])])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert error is None
    assert text == ""
    assert transport.calls


def test_cap_and_incomplete_page(tmp_path):
    nodes = [issue(f"LIN-{index}", due="2026-10-01") for index in range(3)]
    transport = Transport([page(nodes, has_next=True, cursor="c1")])
    text, error = service.digest_stdout(deps(tmp_path, transport, show_limit=1, max_pages=1))
    assert error is None
    lines = [line for line in text.splitlines() if line]
    assert lines[0] == "Overdue (3)"
    assert lines[1].startswith("LIN-0\t")
    assert "+2 more" in lines
    assert lines[-1] == "Later pages were not read, so this is not the full list."
    assert "Due today" not in text


def test_second_page_is_read_and_cursor_is_not_repeated(tmp_path):
    transport = Transport([
        page([issue("LIN-1", due="2026-10-01")], has_next=True, cursor="c1"),
        page([issue("LIN-2", due="2026-10-02")], has_next=False),
    ])
    text, error = service.digest_stdout(deps(tmp_path, transport, show_limit=10))
    assert error is None
    assert "LIN-1" in text and "LIN-2" in text
    assert "Later pages" not in text
    assert len(transport.calls) == 2


def test_repeated_cursor_does_not_loop(tmp_path):
    transport = Transport([
        page([issue("LIN-1")], has_next=True, cursor="c1"),
        page([issue("LIN-2")], has_next=True, cursor="c1"),
    ])
    text, error = service.digest_stdout(deps(tmp_path, transport, max_pages=5))
    assert error is None
    assert len(transport.calls) == 2
    assert "Later pages were not read" in text


def test_nasty_title_stays_on_one_line(tmp_path):
    title = "Ship\r\n**the**\x00 list\x1b[31m [click](https://evil.test)"
    transport = Transport([page([issue("LIN-7", title=title, due=None)])])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert error is None
    rows = [line for line in text.splitlines() if line.startswith("LIN-7\t")]
    assert len(rows) == 1
    assert "\x00" not in text and "\x1b" not in text
    assert "Ship **the** list [31m [click](https://evil.test)" in rows[0]
    assert rows[0].split("\t")[-1].startswith("https://linear.app/")
    assert "Other (1)" in text


def test_refuses_a_non_linear_url_without_fetching_it(tmp_path):
    transport = Transport([page([issue("LIN-7", url="https://evil.test/issue/LIN-7")])])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert error is None
    assert text.endswith("url refused")
    assert "evil.test" not in text
    assert transport.calls[0][0] == "https://api.linear.app/graphql"


def test_missing_key_does_not_call(tmp_path):
    transport = Transport([page([])])
    box = deps(tmp_path, transport)
    box.secrets = lambda name: (None, None)
    text, error = service.digest_stdout(box)
    assert text == ""
    assert error == client.MESSAGES["missing_key"]
    assert transport.calls == []
    assert KEY not in error


def test_bad_key_and_bad_rehearsal_do_not_call(tmp_path):
    transport = Transport([page([])])
    box = deps(tmp_path, transport)
    box.secrets = lambda name: ("bad\nkey", None) if name == "LINEAR_API_KEY" else (None, None)
    _text, error = service.digest_stdout(box)
    assert error == client.MESSAGES["bad_key"]
    assert transport.calls == []
    box.secrets = lambda name: (KEY, None) if name == "LINEAR_API_KEY" else ("http://169.254.169.254/latest", None)
    _text, error = service.digest_stdout(box)
    assert error == client.MESSAGES["rehearsal"]
    assert transport.calls == []
    for raw in ("http://0.0.0.0:9/graphql", "http://192.0.2.1:9/graphql", "https://127.0.0.1:9/graphql", "http://evil.test/graphql"):
        box.secrets = lambda name, raw=raw: (KEY, None) if name == "LINEAR_API_KEY" else (raw, None)
        _text, error = service.digest_stdout(box)
        assert error == client.MESSAGES["rehearsal"]
    assert transport.calls == []


def test_graphql_errors_are_not_an_empty_success(tmp_path):
    body = {"errors": [{"message": KEY, "extensions": {"code": "AUTHENTICATION_ERROR"}}], "data": {"viewer": None}}
    transport = Transport([body])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert text == ""
    assert error == client.MESSAGES["graphql"]
    assert KEY not in error


def test_statuses_do_not_echo_the_key(tmp_path):
    for status, code in ((401, "http_401"), (429, "http_429"), (503, "http_5xx")):
        transport = Transport([{"errors": [{"message": KEY}]}], status=status)
        text, error = service.digest_stdout(deps(tmp_path, transport))
        assert text == ""
        assert KEY not in (error or "")
        if status == 503:
            assert "503" in error
        else:
            assert error == client.MESSAGES[code]


def test_unknown_tool_arguments_fetch_nothing(tmp_path):
    transport = Transport([page([])])
    box = deps(tmp_path, transport)
    assert service.linear_digest(box, {"stale_days": 1}) == "Unknown arguments were rejected. Nothing was fetched."
    assert transport.calls == []
    assert service.linear_digest(box, {}) == ""


def test_read_does_not_write(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    before = sorted(path.name for path in data.iterdir())
    transport = Transport([page([issue("LIN-1")])])
    service.linear_digest(deps(tmp_path, transport), {})
    after = sorted(path.name for path in data.iterdir())
    assert before == after == []


def test_unreadable_due_date_is_not_called_none(tmp_path):
    transport = Transport([page([issue("LIN-1", due="tomorrow")])])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert error is None
    assert "due unreadable" in text
    assert "due none" not in text


def test_boundaries_unicode_html_cap_and_urls(tmp_path):
    long_title = "あ" * 200
    nodes = [
        issue("LIN-1", title=long_title, due=None),
        issue("LIN-2", title="<!-- only a comment -->", due=None),
        issue("LIN-3", title="café 課題", due="2026-10-29"),
        issue("LIN-4", url="https://LINEAR.APP/acme/issue/LIN-4", due=None),
        issue("LIN-5", url="https://user:secret@linear.app/acme/issue/LIN-5", due=None),
        issue("LIN-6", url="http://linear.app/acme/issue/LIN-6", due=None),
        issue("LIN-7", url="https://linear.app/" + ("a" * 500), due=None),
        issue("LIN-8", due=None, kind="duplicate"),
        issue("LIN-10", due=None, kind="triage", state="Triage"),
    ]
    transport = Transport([page(nodes)])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert error is None
    assert "LIN-8" not in text
    assert "Triage" in text
    assert "café 課題" in text
    assert "<!-- only a comment -->" in text
    title = next(line.split("\t")[1] for line in text.splitlines() if line.startswith("LIN-1\t"))
    assert len(title) == 180
    assert title.endswith("...")
    assert "https://LINEAR.APP/acme/issue/LIN-4" in text
    assert "secret" not in text
    assert "http://linear.app" not in text
    assert text.splitlines()[-1].endswith("url refused") or "url refused" in text


def test_show_limit_plus_one(tmp_path):
    nodes = [issue(f"LIN-{index:02d}", due="2026-10-01") for index in range(31)]
    transport = Transport([page(nodes)])
    text, error = service.digest_stdout(deps(tmp_path, transport, show_limit=30))
    assert error is None
    assert text.splitlines()[-1] == "+1 more"
    assert len([line for line in text.splitlines() if "\t" in line]) == 30
    assert "Overdue (31)" in text


def test_saved_stale_days_changes_the_quiet_group(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text('{"stale_days": 14}', encoding="utf-8")
    node = issue("LIN-1", due=None, updated="2026-10-20T00:00:00.000Z")
    text, error = service.digest_stdout(deps(tmp_path, Transport([page([node])])))
    assert error is None
    assert "Other (1)" in text
    assert "Not updated" not in text
    assert any(line.startswith("LIN-1\t") for line in text.splitlines())
    (data / "settings.json").write_text('{"stale_days": true}', encoding="utf-8")
    transport = Transport([page([node])])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert text == ""
    assert "could not be read" in error
    assert transport.calls == []


def test_empty_key_and_bad_node(tmp_path):
    transport = Transport([page([])])
    box = deps(tmp_path, transport)
    box.secrets = lambda name: ("", None) if name == "LINEAR_API_KEY" else (None, None)
    text, error = service.digest_stdout(box)
    assert text == ""
    assert error == client.MESSAGES["missing_key"]
    assert transport.calls == []
    broken = Transport([page([{"identifier": "LIN-1"}])])
    text, error = service.digest_stdout(deps(tmp_path, broken))
    assert text == ""
    assert error == client.MESSAGES["bad_body"]


def test_one_issue_and_failure_catalog():
    assert client.MESSAGES["http_401"].startswith("Linear refused")
    assert all(row["ok"] is False and isinstance(row["error_type"], str) for row in client.FAILURES)


def test_corrupt_settings_do_not_fetch(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text("{", encoding="utf-8")
    transport = Transport([page([])])
    text, error = service.digest_stdout(deps(tmp_path, transport))
    assert text == ""
    assert "could not be read" in error
    assert transport.calls == []


def _hermes_clock():
    try:
        import hermes_time
    except ModuleNotFoundError:
        return None
    return hermes_time


def _restore_clock(old_tz, old_name):
    if old_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old_tz
    if old_name is None:
        os.environ.pop("HERMES_TIMEZONE", None)
    else:
        os.environ["HERMES_TIMEZONE"] = old_name
    time.tzset()
    clock = _hermes_clock()
    if clock is not None:
        clock.reset_cache()


def test_today_and_quiet_use_the_hermes_clock(tmp_path):
    """Tokyo 08:00 is still the previous UTC date. Kiritimati must not win."""
    old_tz = os.environ.get("TZ")
    old_name = os.environ.get("HERMES_TIMEZONE")
    os.environ["HERMES_TIMEZONE"] = "Asia/Tokyo"
    os.environ["TZ"] = "Pacific/Kiritimati"
    time.tzset()
    clock = _hermes_clock()
    try:
        if clock is not None:
            clock.reset_cache()
            assert clock.get_timezone_name() == "Asia/Tokyo"
        instant = datetime(2026, 10, 28, 23, 0, tzinfo=timezone.utc)
        assert instant.astimezone(ZoneInfo("Asia/Tokyo")).date() == date(2026, 10, 29)
        assert instant.date() == date(2026, 10, 28)
        nodes = [
            issue("LIN-A", title="LIN-A", kind="unstarted", due="2026-10-27", updated="2026-10-20T00:00:00.000Z"),
            issue("LIN-B", title="LIN-B", kind="unstarted", due="2026-10-28", updated="2026-10-28T12:00:00.000Z"),
            issue("LIN-C", title="LIN-C", kind="unstarted", due="2026-10-29", updated="2026-10-29T01:00:00.000Z"),
            issue("LIN-D", title="LIN-D", kind="unstarted", due=None, updated="2026-09-01T00:00:00.000Z"),
            issue("LIN-E", title="LIN-E", kind="unstarted", due=None, updated="2026-10-29T00:00:00.000Z"),
        ]
        text, error = service.digest_stdout(deps(tmp_path, Transport([page(nodes)]), today=None, now=instant))
        assert error is None, error
        ids = [line.split("\t")[0] for line in text.splitlines() if "\t" in line]
        assert ids == ["LIN-A", "LIN-B", "LIN-C", "LIN-D", "LIN-E"]
        assert "Overdue (2)" in text
        assert "Due today (1)" in text
        assert "Not updated for 7 days (1)" in text
        assert "Other (1)" in text
        assert text.index("LIN-C") < text.index("LIN-D")
        later = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        assert later.astimezone(ZoneInfo("Pacific/Kiritimati")).date() == date(2026, 10, 30)
        assert later.astimezone(ZoneInfo("Asia/Tokyo")).date() == date(2026, 10, 29)
        one = issue("LIN-C", title="LIN-C", kind="unstarted", due="2026-10-29", updated="2026-10-29T01:00:00.000Z")
        text2, error2 = service.digest_stdout(deps(tmp_path, Transport([page([one])]), today=None, now=later))
        assert error2 is None, error2
        assert "Due today (1)" in text2
        assert "Overdue" not in text2
        assert "Not updated" not in text2
        assert "Other" not in text2
    finally:
        _restore_clock(old_tz, old_name)


def test_config_timezone_when_env_is_unset(tmp_path, isolated_home):
    if _hermes_clock() is None:
        pytest.importorskip("hermes_constants")
    old_tz = os.environ.get("TZ")
    old_name = os.environ.get("HERMES_TIMEZONE")
    os.environ.pop("HERMES_TIMEZONE", None)
    os.environ["TZ"] = "Pacific/Kiritimati"
    (isolated_home / "config.yaml").write_text("timezone: Asia/Tokyo\n", encoding="utf-8")
    time.tzset()
    clock = _hermes_clock()
    if clock is not None:
        clock.reset_cache()
    try:
        later = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        one = issue("LIN-C", title="LIN-C", kind="unstarted", due="2026-10-29", updated="2026-10-29T01:00:00.000Z")
        text, error = service.digest_stdout(deps(tmp_path, Transport([page([one])]), today=None, now=later))
        assert error is None, error
        if clock is not None:
            assert clock.get_timezone_name() == "Asia/Tokyo"
        assert "Due today (1)" in text
        assert "Overdue" not in text
    finally:
        _restore_clock(old_tz, old_name)
