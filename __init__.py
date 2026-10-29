"""Morning list of open Linear issues assigned to you."""
from __future__ import annotations

from pathlib import Path


def register(ctx) -> None:
    if __package__:
        from . import gates
        from .cli import handle, setup_parser
        from .service import TOOLSET, linear_digest
        from .service import Deps
    else:
        import gates
        from cli import handle, setup_parser
        from service import TOOLSET, linear_digest
        from service import Deps

    try:
        reported = ctx.profile_name
    except Exception:
        reported = None
    gates.remember_profile_name(reported)

    def _digest(args, **_kwargs):
        return linear_digest(Deps(), args or {})

    ctx.register_tool(
        name="linear_digest",
        toolset=TOOLSET,
        schema={
            "name": "linear_digest",
            "description": (
                "Lists open Linear issues assigned to you. "
                "Morning list of overdue, due today, not updated recently, and the rest, "
                "each with identifier, title, state, due date, and URL. "
                "Same text as the no-agent cron script. Empty when none are open."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        },
        handler=_digest,
    )
    ctx.register_cli_command(
        name="linear-digest",
        help="Schedule a model-free morning list of your assigned Linear issues",
        setup_fn=setup_parser,
        handler_fn=handle,
        description="Save one no-agent cron job from this machine. Does not register a slash command.",
    )
    try:
        ctx.register_skill(
            "linear-issues",
            Path(__file__).resolve().parent / "skills" / "linear-issues" / "SKILL.md",
            description=(
                "Open Linear issues assigned to you. "
                "Find linear_digest with tool_search and call it for the morning list."
            ),
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("linear-digest skill was not registered", exc_info=True)
