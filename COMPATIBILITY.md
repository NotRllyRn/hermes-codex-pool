# Compatibility

## Baseline

`hermes-codex-pool` **0.1.0** is built from the Hermes Agent **0.20.0** contract at upstream commit `3c27eb6234bf91b8ceee9e9071591b31e9b148cb` (released 2026-08-03).

The same API signatures were source-checked on 2026-08-19 against:

- Hermes Agent **0.20.4**
- upstream `main` commit `395c70d616f6426e990632ff8b57cf1e9499702f`
- Python requirement `>=3.11,<3.14`

This file is the update checkpoint. Change the commit, package version, date, and matrix whenever compatibility is revalidated.

## Compatibility matrix

| Plugin | Hermes | Status | Verification |
| --- | --- | --- | --- |
| 0.1.0 | 0.20.0 | Verified | Real-API import, credential construction, registration + unit tests |
| 0.1.0 | 0.20.4 | Verified | Plugin Doctor + temporary-home native CRUD at `395c70d6` + unit tests |

A live Codex login, token refresh, quota exhaustion, and provider failover require real accounts and are not exercised by the unit suite.

## Internal API boundary

Plugin registration is documented by Hermes. Credential management necessarily uses implementation-level APIs:

- `agent.credential_pool.PooledCredential`
- `agent.credential_pool.load_pool`
- `CredentialPool.entries`, `resolve_target`, `add_entry`, and `remove_index`
- `hermes_cli.auth.write_credential_pool`
- `hermes_cli.auth.mark_provider_active_if_unset`
- `agent.account_usage.fetch_account_usage`

All imports are grouped at the top of `codex_pool.py`. Missing symbols produce a sanitized compatibility message instead of exposing an import traceback through a command.

## Breaking-change watch list

Treat any of the following Hermes changes as a compatibility review blocker:

1. Removal or signature changes to an internal API listed above.
2. Changes to `PooledCredential` fields, `to_dict()`, `runtime_api_key`, or `runtime_base_url`.
3. Changes to the `manual:device_code` source semantics or OAuth ownership/refresh behavior.
4. Changes to `write_credential_pool` locking, merge behavior, or intentional-removal handling.
5. Changes to Codex usage snapshot fields (`available`, `windows`, `label`, `used_percent`, `reset_at`).
6. Changes to `PluginContext.register_cli_command` or `register_command` callback contracts.
7. Changes to Codex's default base URL or provider ID.

Do not work around these by reading or writing `auth.json` directly. Adapt the small compatibility boundary and rerun all tests against the new Hermes source.

## Plugin breaking changes

The following require a plugin major-version bump and a `BREAKING` changelog entry:

- removing or renaming a command;
- making a read-only slash path mutating;
- changing credential storage away from Hermes' native pool;
- changing target semantics away from Hermes ID/label/index resolution;
- accepting secrets through command arguments;
- taking ownership of request routing or refresh behavior.

An internal Hermes adaptation that preserves the public command and storage behavior is not itself a plugin breaking change.
