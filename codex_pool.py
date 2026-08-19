"""Manage Hermes' native OpenAI Codex credential pool."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Callable

PROVIDER = "openai-codex"
INCOMPATIBLE = (
    "Hermes credential APIs expected by hermes-codex-pool are unavailable. "
    "Update the plugin or use a supported Hermes version."
)


class CompatibilityError(RuntimeError):
    """The installed Hermes does not expose the required API boundary."""


# HERMES INTERNAL API BOUNDARY: review COMPATIBILITY.md before changing the
# supported Hermes version. Missing/renamed symbols must fail without secrets.
try:
    _account_usage = import_module("agent.account_usage")
    _pool = import_module("agent.credential_pool")
    _auth = import_module("hermes_cli.auth")
    _prompts = import_module("hermes_cli.secret_prompt")

    fetch_account_usage = _account_usage.fetch_account_usage
    AUTH_TYPE_OAUTH = _pool.AUTH_TYPE_OAUTH
    SOURCE_MANUAL_DEVICE_CODE = _pool.SOURCE_MANUAL_DEVICE_CODE
    STATUS_DEAD = _pool.STATUS_DEAD
    STATUS_EXHAUSTED = _pool.STATUS_EXHAUSTED
    PooledCredential = _pool.PooledCredential
    get_pool_strategy = _pool.get_pool_strategy
    load_pool = _pool.load_pool
    DEFAULT_CODEX_BASE_URL = _auth.DEFAULT_CODEX_BASE_URL
    mark_provider_active_if_unset = _auth.mark_provider_active_if_unset
    write_credential_pool = _auth.write_credential_pool
    masked_secret_prompt = _prompts.masked_secret_prompt
except (ImportError, AttributeError) as exc:  # Keep registration available so commands fail clearly.
    _IMPORT_ERROR: Exception | None = exc
    AUTH_TYPE_OAUTH = "oauth"
    SOURCE_MANUAL_DEVICE_CODE = "manual:device_code"
    STATUS_DEAD = "dead"
    STATUS_EXHAUSTED = "exhausted"
    DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

    def _missing(*_args, **_kwargs):
        raise CompatibilityError(INCOMPATIBLE)

    PooledCredential = fetch_account_usage = get_pool_strategy = load_pool = _missing
    mark_provider_active_if_unset = write_credential_pool = masked_secret_prompt = _missing
else:
    _IMPORT_ERROR = None


def _require_hermes() -> None:
    if _IMPORT_ERROR is not None:
        raise CompatibilityError(INCOMPATIBLE) from None


def setup_cli(parser) -> None:
    """Configure ``hermes codex-pool`` arguments."""
    subs = parser.add_subparsers(dest="codex_pool_action")

    add = subs.add_parser("add", help="Import an OAuth token pair")
    add.add_argument("--label")

    subs.add_parser("list", help="List local pool entries")

    status = subs.add_parser("status", help="Show per-account usage")
    status.add_argument("target", nargs="?")

    rename = subs.add_parser("rename", help="Rename an entry")
    rename.add_argument("target")
    rename.add_argument("new_label")

    remove = subs.add_parser("remove", help="Remove an entry")
    remove.add_argument("target")
    remove.add_argument("--yes", action="store_true")


def handle_cli(args) -> None:
    """Dispatch the terminal-only command tree."""
    try:
        action = getattr(args, "codex_pool_action", None) or "status"
        if action == "add":
            result = add_account(getattr(args, "label", None))
        elif action == "list":
            result = list_accounts()
        elif action == "status":
            result = status_accounts(getattr(args, "target", None))
        elif action == "rename":
            result = rename_account(args.target, args.new_label)
        elif action == "remove":
            result = remove_account(args.target, yes=args.yes)
        else:  # argparse should make this unreachable.
            result = "Unknown codex-pool action."
    except CompatibilityError:
        result = INCOMPATIBLE
    except Exception:
        result = "Codex pool command failed. Credential details were not displayed."
    print(result)


def _label_exists(entries, label: str, *, exclude_id: str | None = None) -> bool:
    wanted = label.casefold()
    return any(entry.id != exclude_id and entry.label.strip().casefold() == wanted for entry in entries)


def _default_label(entries) -> str:
    number = len(entries) + 1
    while _label_exists(entries, f"openai-codex-oauth-{number}"):
        number += 1
    return f"openai-codex-oauth-{number}"


def _local_state(entry) -> str:
    if entry.last_status == STATUS_DEAD:
        return "dead"
    if entry.last_status == STATUS_EXHAUSTED:
        return "exhausted"
    return "available"


def add_account(label: str | None = None) -> str:
    """Import a masked access/refresh token pair into Hermes."""
    _require_hermes()
    pool = load_pool(PROVIDER)
    entries = pool.entries()
    label = (label or "").strip() or _default_label(entries)
    if _label_exists(entries, label):
        return f'Label "{label}" already exists. Use a unique label.'

    access = masked_secret_prompt("Access token: ").strip()
    refresh = masked_secret_prompt("Refresh token: ").strip()
    if not access:
        return "Access token is required."
    if not refresh:
        return "Refresh token is required."
    if any(entry.access_token == access and entry.refresh_token == refresh for entry in entries):
        return "That token pair is already present in the Codex pool."

    entry = PooledCredential(
        provider=PROVIDER,
        id=uuid.uuid4().hex[:6],
        label=label,
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source=SOURCE_MANUAL_DEVICE_CODE,
        access_token=access,
        refresh_token=refresh,
        base_url=DEFAULT_CODEX_BASE_URL,
        last_refresh=None,
    )
    added = pool.add_entry(entry)
    if not entries:
        mark_provider_active_if_unset(PROVIDER)
    return f'Added Codex credential "{added.label}" ({added.id}).'


def list_accounts() -> str:
    """List local entries without provider requests."""
    _require_hermes()
    entries = load_pool(PROVIDER).entries()
    if not entries:
        return "No Codex credentials found. Add one with: hermes codex-pool add"
    lines = [f"OpenAI Codex credential pool · strategy: {get_pool_strategy(PROVIDER)}", ""]
    lines.extend(
        f"#{index}  {entry.id}  {entry.label}  {entry.auth_type}  {_local_state(entry)}"
        for index, entry in enumerate(entries, 1)
    )
    return "\n".join(lines)


def _reset_text(reset_at: datetime | None) -> str:
    if reset_at is None:
        return ""
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    try:
        seconds = max(0, int((reset_at - datetime.now(timezone.utc)).total_seconds()))
    except (OverflowError, TypeError, ValueError):
        return ""
    minutes = seconds // 60
    if minutes >= 1440:
        days, hours = divmod(minutes // 60, 24)
        relative = f"{days}d {hours}h"
    elif minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        relative = f"{hours}h {minutes}m"
    else:
        relative = f"{minutes}m"
    return f" · resets in {relative}" if seconds else " · resets now"


def _format_usage(snapshot: Any) -> list[str]:
    if snapshot is None or not getattr(snapshot, "available", True):
        return ["usage unavailable"]
    lines = []
    for window in getattr(snapshot, "windows", ()):
        used = getattr(window, "used_percent", None)
        if used is None:
            line = f"{window.label} unavailable"
        else:
            try:
                remaining = max(0, round(100 - float(used)))
            except (TypeError, ValueError):
                remaining = None
            line = f"{window.label} {remaining}% left" if remaining is not None else f"{window.label} unavailable"
        lines.append(line + _reset_text(getattr(window, "reset_at", None)))
    return lines or ["usage unavailable"]


def _fetch_usage(entry):
    return fetch_account_usage(
        PROVIDER,
        base_url=entry.runtime_base_url or DEFAULT_CODEX_BASE_URL,
        api_key=entry.runtime_api_key,
    )


def _weekly_window(snapshot):
    if snapshot is None or not getattr(snapshot, "available", True):
        return None
    return next(
        (window for window in getattr(snapshot, "windows", ()) if str(window.label).casefold() == "weekly"),
        None,
    )


def _weekly_reset(window) -> datetime | None:
    reset = getattr(window, "reset_at", None)
    if not isinstance(reset, datetime):
        return None
    return reset.replace(tzinfo=timezone.utc) if reset.tzinfo is None else reset.astimezone(timezone.utc)


def preferred_credential_id(provider: str) -> str | None:
    """Choose the healthy Codex entry whose Weekly window resets first."""
    if provider != PROVIDER:
        return None
    try:
        entries = [entry for entry in load_pool(PROVIDER).entries() if _local_state(entry) == "available"]
        ranked = []
        for index, entry in enumerate(entries):
            reset = _weekly_reset(_weekly_window(_fetch_usage(entry)))
            if reset is None:
                return None
            ranked.append((reset, index, entry.id))
        return min(ranked)[2] if ranked else None
    except Exception:
        return None


def _weekly_summary(entry) -> str:
    try:
        window = _weekly_window(_fetch_usage(entry))
        if window is None:
            return "Weekly usage unavailable"
        used = float(window.used_percent)
        return f"Weekly {max(0, round(100 - used))}% left{_reset_text(window.reset_at)}"
    except (AttributeError, TypeError, ValueError):
        return "Weekly usage unavailable"


def build_hook_handlers(emit: Callable[[str], Any] = print):
    """Build fail-open selection and truthful request-observer callbacks."""
    preferred: dict[str, str] = {}
    shown: dict[str, None] = {}

    def remember(mapping: dict, key: str, value) -> None:
        mapping[key] = value
        if len(mapping) > 256:
            mapping.pop(next(iter(mapping)))

    def select_hook(**payload):
        if payload.get("provider") != PROVIDER:
            return None
        session_id = str(payload.get("session_id") or "")
        credential_id = preferred_credential_id(PROVIDER)
        preferred.pop(session_id, None)
        if credential_id:
            remember(preferred, session_id, credential_id)
            return {"credential_id": credential_id}
        return None

    def request_hook(**payload):
        if payload.get("provider") != PROVIDER:
            return None
        turn_id = str(payload.get("turn_id") or "")
        credential_id = str(payload.get("credential_id") or "")
        if not turn_id or not credential_id or turn_id in shown:
            return None
        remember(shown, turn_id, None)
        session_id = str(payload.get("session_id") or "")
        intended = preferred.pop(session_id, None)
        try:
            entry = next(
                (entry for entry in load_pool(PROVIDER).entries() if entry.id == credential_id),
                None,
            )
            label = str(payload.get("credential_label") or getattr(entry, "label", None) or credential_id)
            usage = _weekly_summary(entry) if entry is not None else "Weekly usage unavailable"
            reason = "earliest weekly reset" if intended == credential_id else "Hermes selected available credential"
            emit(f"[Codex: {label} · {usage} · {reason}]")
        except Exception:
            return None
        return None

    return select_hook, request_hook


def setup_hooks(ctx) -> None:
    select_hook, request_hook = build_hook_handlers()
    ctx.register_hook("pre_credential_select", select_hook)
    ctx.register_hook("pre_api_request", request_hook)


def status_accounts(target: str | None = None) -> str:
    """Show local state and provider-reported usage for each selected entry."""
    _require_hermes()
    pool = load_pool(PROVIDER)
    entries = list(enumerate(pool.entries(), 1))
    if not entries:
        return "No Codex credentials found. Add one with: hermes codex-pool add"
    if target:
        index, entry, error = pool.resolve_target(target)
        if error:
            return error
        entries = [(index, entry)]

    lines = [f"OpenAI Codex credential pool · strategy: {get_pool_strategy(PROVIDER)}", ""]
    for index, entry in entries:
        lines.append(f"#{index}  {entry.label}  {_local_state(entry)}")
        try:
            snapshot = _fetch_usage(entry)
        except Exception:
            snapshot = None
        lines.extend(f"    {line}" for line in _format_usage(snapshot))
    lines.extend(("", "Hermes owns selection, refresh, and automatic failover."))
    return "\n".join(lines)


def rename_account(target: str, new_label: str) -> str:
    """Rename one entry while preserving every other persisted field."""
    label = new_label.strip()
    if not label:
        return "Label cannot be empty."
    _require_hermes()
    pool = load_pool(PROVIDER)
    _index, entry, error = pool.resolve_target(target)
    if error:
        return error
    entries = pool.entries()
    if _label_exists(entries, label, exclude_id=entry.id):
        return f'Label "{label}" already exists. Use a unique label.'
    updated = [replace(item, label=label) if item.id == entry.id else item for item in entries]
    # Internal writer preserves Hermes' auth-store lock and concurrent merges.
    write_credential_pool(PROVIDER, [item.to_dict() for item in updated])
    return f'Renamed "{entry.label}" to "{label}".'


def remove_account(target: str, *, yes: bool = False) -> str:
    """Remove one native pool row after terminal confirmation."""
    _require_hermes()
    pool = load_pool(PROVIDER)
    index, entry, error = pool.resolve_target(target)
    if error:
        return error
    if not yes:
        answer = input(f'Remove Codex credential "{entry.label}" ({entry.id})? [y/N]: ')
        if answer.strip().lower() not in {"y", "yes"}:
            return "Cancelled."
    pool.remove_index(index)
    return f'Removed "{entry.label}".'


def handle_slash(raw_args: str) -> str:
    """Expose status only to in-session and gateway callers."""
    action = raw_args.strip().lower()
    if action in {"", "status"}:
        try:
            return status_accounts()
        except CompatibilityError:
            return INCOMPATIBLE
        except Exception:
            return "Codex pool status unavailable. Credential details were not displayed."
    if action == "help":
        return "Usage: /codex-pool [status|help]"
    return "Credential changes are terminal-only. Use: hermes codex-pool --help"
