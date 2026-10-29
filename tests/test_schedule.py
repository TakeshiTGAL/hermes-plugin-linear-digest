"""Schedule writes a no-agent script and does not fetch."""
from __future__ import annotations

import pytest

pytest.importorskip("hermes_constants")

import gates
import public_cron
import service


class FakeCron:
    def __init__(self):
        self.jobs = []

    def load_jobs(self):
        return list(self.jobs)

    def get_job(self, job_id):
        for job in self.jobs:
            if job.get("id") == job_id:
                return job
        return None

    def create_job(self, **kwargs):
        job = {"id": "job1"}
        job.update(kwargs)
        self.jobs.append(job)
        return job

    def update_job(self, job_id, updates):
        job = self.get_job(job_id)
        if job is None:
            return None
        job.update(updates)
        return job

    def remove_job(self, job_id):
        self.jobs = [job for job in self.jobs if job.get("id") != job_id]
        return True


def test_schedule_status_unschedule_do_not_fetch(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    deps.transport = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fetched"))
    code, text = service.schedule(deps, "local", "7")
    assert code == 0, text
    assert deps.cron.jobs[0]["no_agent"] is True
    assert deps.cron.jobs[0]["script"] == service.SCRIPT_NAME
    assert deps.cron.jobs[0]["schedule"] == "0 8 * * *"
    script = isolated_home / "scripts" / service.SCRIPT_NAME
    assert script.is_file()
    source = script.read_text(encoding="utf-8")
    assert "digest_stdout" in source
    assert "get_timezone_name" in source
    assert 'os.environ["TZ"]' in source
    compile(source, str(script), "exec")
    create_lines = [line for line in text.splitlines() if "cron create" in line]
    assert len(create_lines) == 1
    assert "--no-agent" in create_lines[0]
    assert "--script linear-digest.py" in create_lines[0]
    assert '"0 8 * * *"' in create_lines[0]
    assert create_lines[0].strip() != "hermes cron create"
    code, status = service.status(deps)
    assert "--no-agent" in status
    assert "--script linear-digest.py" in status
    assert code == 0, status
    assert "does not fetch" in status
    code, gone = service.unschedule(deps)
    assert code == 0, gone
    assert not script.exists()
    assert deps.cron.jobs == []


def test_external_remove_is_called_gone(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    code, text = service.schedule(deps, "local", None)
    assert code == 0, text
    deps.cron.jobs.clear()
    code, text = service.status(deps)
    assert code == 0, text
    assert "gone" in text.lower()
    code, text = service.unschedule(deps)
    assert code == 0, text
    assert "already gone" in text.lower()


def test_rejected_deliver_and_stale_days_change_nothing(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    for deliver in ("monday", "5m", "cli", "cron"):
        code, text = service.schedule(deps, deliver, None)
        assert code == 1, text
        assert deps.cron.jobs == []
    for stale in ("0", "91", "-3", "7.5", "seven"):
        code, text = service.schedule(deps, "local", stale)
        assert code == 1, text
        assert "1 to 90" in text
    script = isolated_home / "scripts" / service.SCRIPT_NAME
    assert not script.exists()


def test_write_denial_does_not_create_a_job(tmp_path, isolated_home, monkeypatch):
    monkeypatch.setattr(gates, "write_guard_error", lambda _path: "Write denied: protected")
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    code, text = service.schedule(deps, "local", None)
    assert code == 1
    assert "denied" in text.lower()
    assert deps.cron.jobs == []
    script = isolated_home / "scripts" / service.SCRIPT_NAME
    assert not script.exists()


def test_status_prints_the_live_cron_row(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    code, text = service.schedule(deps, "local", None)
    assert code == 0, text
    deps.cron.jobs[0]["schedule"] = "0 9 * * *"
    deps.cron.jobs[0]["deliver"] = "telegram"
    code, text = service.status(deps)
    assert code == 0, text
    assert "0 9 * * *" in text
    assert "Deliver telegram." in text
    assert "Saved deliver is local." in text
    assert "08:00" not in text


def test_duplicate_names_do_not_write_the_script(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    deps.cron.jobs = [
        {"id": "a", "name": "linear-digest"},
        {"id": "b", "name": "linear-digest"},
    ]
    code, text = service.schedule(deps, "local", None)
    assert code == 1, text
    assert "Nothing was changed" in text
    script = isolated_home / "scripts" / service.SCRIPT_NAME
    assert not script.exists()
    assert [job["id"] for job in deps.cron.jobs] == ["a", "b"]


def test_saved_id_does_not_override_a_second_named_job(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    code, text = service.schedule(deps, "local", "7")
    assert code == 0, text
    deps.cron.jobs.append({"id": "other", "name": "linear-digest", "schedule": "0 1 * * *"})
    code, text = service.schedule(deps, "local", None)
    assert code == 1, text
    assert "Nothing was changed" in text
    saved = next(job for job in deps.cron.jobs if job["id"] == "job1")
    assert saved["schedule"] == "0 8 * * *"
    other = next(job for job in deps.cron.jobs if job["id"] == "other")
    assert other["schedule"] == "0 1 * * *"


def test_failed_create_removes_the_script(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()

    def boom(**_kwargs):
        raise RuntimeError("could not create")

    deps.cron.create_job = boom
    code, text = service.schedule(deps, "local", None)
    assert code == 1, text
    script = isolated_home / "scripts" / service.SCRIPT_NAME
    assert not script.exists()


def test_unschedule_leaves_the_script_when_another_job_shares_the_name(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    code, text = service.schedule(deps, "local", None)
    assert code == 0, text
    deps.cron.jobs.append({"id": "other", "name": "linear-digest"})
    script = isolated_home / "scripts" / service.SCRIPT_NAME
    assert script.is_file()
    code, text = service.unschedule(deps)
    assert code == 0, text
    assert "left" in text.lower()
    assert script.is_file()
    assert any(job["id"] == "other" for job in deps.cron.jobs)


def test_omitted_stale_days_keeps_the_saved_value(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    code, text = service.schedule(deps, "local", "14")
    assert code == 0, text
    assert "stale days 14" in text
    code, text = service.schedule(deps, "local", None)
    assert code == 0, text
    assert "stale days 14" in text


def test_bot_chat_schedule_does_not_claim_zero_tokens(tmp_path, isolated_home):
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.cron = FakeCron()
    code, text = service.schedule(deps, "bot-chat", None)
    assert code == 0, text
    assert "0 tokens" not in text
    assert "model turn" in text


def test_real_hermes_schedule_list_unschedule(tmp_path, monkeypatch):
    if public_cron.hermes_binary() is None:
        import pytest
        pytest.skip("hermes is not on PATH")
    user = tmp_path / "person"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    deps = service.Deps()
    deps.data_dir = user / ".hermes" / "plugin-data" / "linear-digest"
    code, text = service.schedule(deps, "local", None)
    assert code == 0, text
    script = user / ".hermes" / "scripts" / service.SCRIPT_NAME
    assert script.is_file()
    listed = public_cron.default_run(["list", "--all"])
    assert listed.returncode == 0, listed.stderr
    assert "no-agent" in listed.stdout
    try:
        code, gone = service.unschedule(deps)
        assert code == 0, gone
        assert not script.exists()
        after = public_cron.default_run(["list", "--all"])
        assert service.JOB_NAME not in after.stdout
    finally:
        if script.exists():
            script.unlink()
