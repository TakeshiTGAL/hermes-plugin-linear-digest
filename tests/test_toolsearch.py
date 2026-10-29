"""Three ordinary requests should rank linear_digest in the top 5."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

QUERIES = [
    "linear issues assigned",
    "overdue linear due today",
    "morning linear issue list",
]


def _load_tools():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("linear_digest_plugin", root / "__init__.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["linear_digest_plugin"] = module
    spec.loader.exec_module(module)

    class Ctx:
        def __init__(self):
            self.tools = []

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def __getattr__(self, _name):
            return lambda *_a, **_k: None

    ctx = Ctx()
    module.register(ctx)
    return ctx.tools


def test_three_phrases_rank_in_the_top_five():
    src = os.environ.get("HERMES_SRC") or os.environ.get("HERMES_AGENT_SRC")
    if not src or not Path(src).is_dir():
        pytest.skip("HERMES_SRC is not set")
    sys.path.insert(0, src)
    from toolsets import _HERMES_CORE_TOOLS
    import model_tools  # noqa: F401
    from tools.registry import registry
    from tools.tool_search_catalog import build_catalog, search_catalog

    tools = _load_tools()
    core = []
    for entry in registry.get_all_entries():
        if entry.name in set(_HERMES_CORE_TOOLS):
            core.append({"type": "function", "function": {**entry.schema, "name": entry.name}})
    for tool in tools:
        registry.register(name=tool["name"], toolset=tool["toolset"], schema=tool["schema"], handler=lambda **_k: "", override=True)
    catalog = build_catalog(core + [{"type": "function", "function": tool["schema"]} for tool in tools])
    for query in QUERIES:
        ranked = [item.name for item in search_catalog(catalog, query, limit=5)]
        assert "linear_digest" in ranked, (query, ranked)
