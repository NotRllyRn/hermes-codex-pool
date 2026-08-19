from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest  # type: ignore[import-not-found]

_ROOT = Path(__file__).parents[1]
_SUBJECT_SPEC = importlib.util.spec_from_file_location("codex_pool", _ROOT / "codex_pool.py")
assert _SUBJECT_SPEC is not None and _SUBJECT_SPEC.loader is not None
subject = importlib.util.module_from_spec(_SUBJECT_SPEC)
sys.modules["codex_pool"] = subject
_SUBJECT_SPEC.loader.exec_module(subject)


@dataclass
class Entry:
    provider: str = subject.PROVIDER
    id: str = "abc123"
    label: str = "work"
    auth_type: str = "oauth"
    priority: int = 0
    source: str = "manual:device_code"
    access_token: str = "access-secret"
    refresh_token: str = "refresh-secret"
    base_url: str = "https://codex.example"
    last_refresh: str | None = None
    last_status: str | None = None
    last_error_message: str | None = None
    request_count: int = 0

    @property
    def runtime_api_key(self):
        return self.access_token

    @property
    def runtime_base_url(self):
        return self.base_url

    def to_dict(self):
        return asdict(self)


class Pool:
    def __init__(self, entries=()):
        self._entries = list(entries)
        self.added = []
        self.removed = []

    def entries(self):
        return list(self._entries)

    def add_entry(self, entry):
        self.added.append(entry)
        return entry

    def remove_index(self, index):
        self.removed.append(index)

    def resolve_target(self, target):
        raw = str(target)
        for index, entry in enumerate(self._entries, 1):
            if raw in {entry.id, entry.label, str(index)}:
                return index, entry, None
        return None, None, f'No credential matching "{raw}".'

    def select(self):
        raise AssertionError("status must not select a credential")


@pytest.fixture(autouse=True)
def hermes_available(monkeypatch):
    monkeypatch.setattr(subject, "_IMPORT_ERROR", None)
    monkeypatch.setattr(subject, "PooledCredential", Entry)
    monkeypatch.setattr(subject, "get_pool_strategy", lambda _provider: "fill_first")


def use_pool(monkeypatch, entries=()):
    pool = Pool(entries)
    monkeypatch.setattr(subject, "load_pool", lambda _provider: pool)
    return pool


def test_registration_and_cli_parser():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "hermes_codex_pool", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    ctx = SimpleNamespace(cli=[], slash=[])
    ctx.register_cli_command = lambda **kwargs: ctx.cli.append(kwargs)
    ctx.register_command = lambda *args, **kwargs: ctx.slash.append((args, kwargs))

    plugin.register(ctx)

    assert ctx.cli[0]["name"] == "codex-pool"
    assert ctx.slash[0][0] == ("codex-pool",)
    parser = argparse.ArgumentParser()
    plugin.setup_cli(parser)
    assert parser.parse_args(["status", "work"]).target == "work"
    assert parser.parse_args(["remove", "1", "--yes"]).yes


def test_add_uses_masked_prompts_and_native_shape(monkeypatch):
    pool = use_pool(monkeypatch)
    prompts = []
    secrets = iter(("access-secret", "refresh-secret"))
    monkeypatch.setattr(subject, "masked_secret_prompt", lambda prompt: prompts.append(prompt) or next(secrets))
    activated = []
    monkeypatch.setattr(subject, "mark_provider_active_if_unset", activated.append)

    result = subject.add_account("work")

    assert prompts == ["Access token: ", "Refresh token: "]
    assert len(pool.added) == 1
    entry = pool.added[0]
    assert (entry.provider, entry.auth_type, entry.source) == (
        "openai-codex",
        "oauth",
        "manual:device_code",
    )
    assert (entry.label, entry.access_token, entry.refresh_token) == (
        "work",
        "access-secret",
        "refresh-secret",
    )
    assert entry.base_url == subject.DEFAULT_CODEX_BASE_URL
    assert activated == [subject.PROVIDER]
    assert "access-secret" not in result and "refresh-secret" not in result


@pytest.mark.parametrize(
    ("secrets", "message"),
    [(('  ', 'refresh-secret'), "Access token is required."), (("access-secret", ""), "Refresh token is required.")],
)
def test_add_rejects_empty_tokens_without_leaking(monkeypatch, secrets, message):
    pool = use_pool(monkeypatch)
    values = iter(secrets)
    monkeypatch.setattr(subject, "masked_secret_prompt", lambda _prompt: next(values))
    monkeypatch.setattr(subject, "mark_provider_active_if_unset", lambda _provider: None)

    result = subject.add_account()

    assert result == message
    assert not pool.added
    assert all(secret.strip() not in result for secret in secrets if secret.strip())


def test_add_rejects_duplicate_label_before_prompt(monkeypatch):
    use_pool(monkeypatch, [Entry(label="Work")])
    monkeypatch.setattr(subject, "masked_secret_prompt", lambda _prompt: pytest.fail("must not prompt"))
    assert "already exists" in subject.add_account(" work ")


def test_add_rejects_duplicate_pair_and_does_not_reactivate(monkeypatch):
    existing = Entry()
    pool = use_pool(monkeypatch, [existing])
    values = iter((existing.access_token, existing.refresh_token))
    monkeypatch.setattr(subject, "masked_secret_prompt", lambda _prompt: next(values))
    monkeypatch.setattr(subject, "mark_provider_active_if_unset", lambda _provider: pytest.fail("not first"))
    assert "already present" in subject.add_account("personal")
    assert not pool.added


def test_list_is_local_only_and_renders_states(monkeypatch):
    entries = [
        Entry(id="one111", label="one"),
        Entry(id="two222", label="two", last_status=subject.STATUS_EXHAUSTED),
        Entry(id="tri333", label="three", last_status=subject.STATUS_DEAD),
    ]
    use_pool(monkeypatch, entries)
    monkeypatch.setattr(subject, "fetch_account_usage", lambda *_a, **_k: pytest.fail("network call"))

    result = subject.list_accounts()

    assert "#1  one111  one  oauth  available" in result
    assert "#2  two222  two  oauth  exhausted" in result
    assert "#3  tri333  three  oauth  dead" in result
    assert "strategy: fill_first" in result


def test_list_empty_pool_has_setup_hint(monkeypatch):
    use_pool(monkeypatch)
    assert subject.list_accounts().endswith("hermes codex-pool add")


def snapshot(used=28):
    window = SimpleNamespace(
        label="Session",
        used_percent=used,
        reset_at=datetime.now(timezone.utc) + timedelta(hours=2, minutes=15),
    )
    weekly = SimpleNamespace(label="Weekly", used_percent=62, reset_at=None)
    return SimpleNamespace(available=True, windows=(window, weekly))


def test_status_fetches_each_explicit_credential_and_renders_usage(monkeypatch):
    entries = [Entry(id="one111", label="one", access_token="token-one"), Entry(id="two222", label="two", access_token="token-two")]
    use_pool(monkeypatch, entries)
    calls = []
    monkeypatch.setattr(subject, "fetch_account_usage", lambda provider, **kwargs: calls.append((provider, kwargs)) or snapshot())

    result = subject.status_accounts()

    assert [call[1]["api_key"] for call in calls] == ["token-one", "token-two"]
    assert all(call[1]["base_url"] == "https://codex.example" for call in calls)
    assert result.count("Session 72% left") == 2
    assert result.count("Weekly 38% left") == 2
    assert "token-one" not in result and "token-two" not in result


def test_status_failure_is_per_entry_and_does_not_mutate(monkeypatch):
    pool = use_pool(monkeypatch, [Entry(label="one"), Entry(id="two222", label="two")])
    calls = iter((RuntimeError("secret detail"), snapshot()))

    def fetch(*_args, **_kwargs):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(subject, "fetch_account_usage", fetch)
    result = subject.status_accounts()
    assert "usage unavailable" in result and "Session 72% left" in result
    assert pool.removed == [] and pool.added == []
    assert "secret detail" not in result


def test_status_target_delegates_to_resolve_target(monkeypatch):
    pool = use_pool(monkeypatch, [Entry(label="one"), Entry(id="two222", label="two")])
    targets = []
    original = pool.resolve_target
    pool.resolve_target = lambda target: targets.append(target) or original(target)
    monkeypatch.setattr(subject, "fetch_account_usage", lambda *_a, **_k: None)
    result = subject.status_accounts("two222")
    assert targets == ["two222"]
    assert "#2  two" in result and "#1  one" not in result


def test_rename_preserves_every_field_except_label(monkeypatch):
    original = Entry(last_status="exhausted", last_error_message="kept", request_count=9)
    use_pool(monkeypatch, [original])
    writes = []
    monkeypatch.setattr(subject, "write_credential_pool", lambda provider, entries: writes.append((provider, entries)))

    result = subject.rename_account("1", " work-main ")

    assert result == 'Renamed "work" to "work-main".'
    before, after = original.to_dict(), writes[0][1][0]
    assert writes[0][0] == subject.PROVIDER
    assert after.pop("label") == "work-main"
    before.pop("label")
    assert after == before


def test_rename_rejects_blank_and_duplicate(monkeypatch):
    pool = use_pool(monkeypatch, [Entry(), Entry(id="two222", label="Personal")])
    monkeypatch.setattr(subject, "write_credential_pool", lambda *_a: pytest.fail("must not write"))
    assert subject.rename_account("work", " ") == "Label cannot be empty."
    assert "already exists" in subject.rename_account("work", "personal")
    assert not pool.added


def test_remove_confirmation_defaults_no(monkeypatch):
    pool = use_pool(monkeypatch, [Entry()])
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert subject.remove_account("work") == "Cancelled."
    assert pool.removed == []


def test_remove_yes_uses_resolved_one_based_index_without_secrets(monkeypatch):
    entry = Entry(id="two222", label="personal", access_token="private-access", refresh_token="private-refresh")
    pool = use_pool(monkeypatch, [Entry(), entry])
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must skip confirmation"))
    result = subject.remove_account("two222", yes=True)
    assert pool.removed == [2]
    assert result == 'Removed "personal".'
    assert "private-access" not in result and "private-refresh" not in result


@pytest.mark.parametrize("args", ["", "status"])
def test_slash_status_is_read_only(monkeypatch, args):
    monkeypatch.setattr(subject, "status_accounts", lambda: "dashboard")
    monkeypatch.setattr(subject, "add_account", lambda *_a: pytest.fail("mutation"))
    monkeypatch.setattr(subject, "rename_account", lambda *_a: pytest.fail("mutation"))
    monkeypatch.setattr(subject, "remove_account", lambda *_a, **_k: pytest.fail("mutation"))
    assert subject.handle_slash(args) == "dashboard"


def test_slash_help_and_mutations():
    assert subject.handle_slash("help") == "Usage: /codex-pool [status|help]"
    assert "terminal-only" in subject.handle_slash("add")
    assert "terminal-only" in subject.handle_slash("remove work")


def test_missing_hermes_api_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr(subject, "_IMPORT_ERROR", ImportError("sensitive path"))
    subject.handle_cli(SimpleNamespace(codex_pool_action="list"))
    output = capsys.readouterr().out
    assert subject.INCOMPATIBLE in output
    assert "sensitive path" not in output
