"""Hermes Codex Pool plugin registration."""

from pathlib import Path
from runpy import run_path

_module = run_path(str(Path(__file__).with_name("codex_pool.py")))
handle_cli = _module["handle_cli"]
handle_slash = _module["handle_slash"]
setup_cli = _module["setup_cli"]


def register(ctx) -> None:
    ctx.register_cli_command(
        name="codex-pool",
        help="Manage OpenAI Codex OAuth pool entries",
        description="Import, list, rename, remove, and inspect Codex accounts.",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
    )
    ctx.register_command(
        "codex-pool",
        handler=handle_slash,
        description="Show OpenAI Codex credential-pool status",
        args_hint="[status|help]",
    )
