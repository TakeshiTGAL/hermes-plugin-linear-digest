import os
import sys
from pathlib import Path

import pytest

_src = os.environ.get("HERMES_SRC") or os.environ.get("HERMES_AGENT_SRC")
if _src and Path(_src).is_dir():
    sys.path.insert(0, _src)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("LINEAR_DIGEST_REHEARSAL", raising=False)
    return home
