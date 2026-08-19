# Compatibility

## Baseline

Credential management was built from the Hermes Agent **0.20.0** contract at upstream commit `3c27eb6234bf91b8ceee9e9071591b31e9b148cb`.

`hermes-codex-pool` **0.2.0** additionally targets the external-plugin contract described in `HERMES_UPSTREAM_CREDENTIAL_HOOKS_PLAN.md`:

- `pre_credential_select` accepts `{"credential_id": "..."}` once per turn;
- `pre_api_request` reports `turn_id`, `credential_id`, and `credential_label`;
- hook callbacks accept additive fields through `**kwargs`.

That contract is assumed to exist on the post-merge Hermes build. Upstream `main` at `fdf6f1d4c80f510c1d579e7fc3b2769f81a97892` (2026-08-19, package 0.20.4) does not yet contain it, so its Plugin Doctor correctly rejects the v0.2.0 manifest. Use plugin v0.1.0 with stock Hermes 0.20.x.

This file is the update checkpoint. Replace the provisional requirement with the first released Hermes version and commit containing the hooks after merge.

## Compatibility matrix

| Plugin | Hermes | Status | Verification |
| --- | --- | --- | --- |
| 0.1.0 | 0.20.0–0.20.4 | Verified | Native API import, Plugin Doctor, temporary-home CRUD, unit tests |
| 0.2.0 | Post-hook merge | Contract-tested | Hook registration/payload, policy, truthful identity, deduplication |

A live Codex login, token refresh, quota exhaustion, provider failover, and merged-upstream Plugin Doctor run remain release checks.

## Internal API boundary

Hook registration and payload evolution are documented Hermes plugin surfaces. Credential management and usage necessarily use implementation-level APIs:

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
6. Changes to `PluginContext.register_cli_command`, `register_command`, or `register_hook` callback contracts.
7. Changes to `pre_credential_select` directive semantics or removal of `turn_id`, `credential_id`, or `credential_label` from `pre_api_request`.
8. Changes to Codex's default base URL or provider ID.

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
