# Hermes Agent upstream plan: plugin credential preference + credential identity

Date reviewed: 2026-08-19

Target upstream: `NousResearch/hermes-agent`

Concrete external consumer: `NotRllyRn/hermes-codex-pool`

## Goal

Add the smallest generic plugin surface needed for an external plugin to:

1. ask Hermes to use a specific credential-pool entry for the next user turn; and
2. observe which credential Hermes actually used on each provider request.

The upstream change must stay provider-agnostic. No Codex quota logic, account ranking, UI, token import, usage fetching, or plugin-specific code belongs in Hermes core.

The external `hermes-codex-pool` plugin remains responsible for computing its policy (for example: choose the healthy Codex account whose Weekly quota resets soonest) and for displaying the selected account/reason to the user.

Hermes remains responsible for OAuth refresh, credential validation, cooldowns, 401/429/billing handling, retries, automatic failover, provider fallback, and client reconstruction.

---

## Why this shape matches Hermes contribution policy

Hermes' development guide says:

- capability should live at the edges and core should stay a narrow waist;
- plugins must not modify core files themselves;
- when a real external plugin needs a capability the framework does not expose, widen the **generic plugin surface** rather than special-casing the plugin;
- hooks are not considered speculative when there is a concrete external consumer;
- documented plugin surfaces should evolve additively;
- hook payload additions should be keyword fields so existing callbacks remain compatible;
- tests should assert behavior, not source shape;
- changes touching runtime resolution should exercise the real path;
- use `scripts/run_tests.sh`, not direct pytest, for CI parity.

This proposal follows those rules exactly: the external plugin stays external, and Hermes gets only a generic credential-selection directive plus non-secret identity metadata.

Primary references:

- https://github.com/NousResearch/hermes-agent/blob/main/AGENTS.md
- https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools

A related existing issue is #6551, which discusses per-account Codex quota visibility and already uses `credential_id` / `credential_label` terminology. It is related context, not the same feature:

- https://github.com/NousResearch/hermes-agent/issues/6551

---

# Upstream change 1: `pre_credential_select` directive hook

## Contract

Add a new plugin hook:

```text
pre_credential_select
```

It fires **once per user turn**, after Hermes has restored the primary runtime from any previous provider fallback and before the first provider API request for that turn.

A plugin may return:

```python
{"credential_id": "abc123"}
```

or `None`.

Semantics:

- first valid directive wins, consistent with Hermes' existing directive/control hook family;
- `credential_id` is an opaque Hermes credential-pool entry ID;
- Hermes validates the requested ID against the current provider's pool;
- only an available/usable entry may be selected;
- a missing, malformed, unknown, dead, or currently exhausted ID is ignored;
- plugin exceptions are fail-open and must not break the turn;
- if no plugin chooses a credential, behavior is exactly unchanged;
- after a requested credential is selected, all existing Hermes failover behavior stays active. A later 401/429/billing failure can still cause Hermes to rotate normally.

The hook does **not** expose access tokens, refresh tokens, API keys, or raw credential objects.

Recommended payload:

```python
{
    "session_id": str,
    "provider": str,
    "model": str,
    "platform": str,
    "current_credential_id": str | None,
}
```

Do not add more fields until a real consumer needs them.

### Why once per turn instead of once per API request

A Hermes turn may contain multiple provider calls because of tool use. Choosing again before every internal tool-loop request would cause unnecessary account switching and provider prompt-cache misses.

The current Hermes hook docs define `pre_llm_call` as once per turn and `pre_api_request` as once per provider attempt. Credential policy belongs at the turn boundary, while observation belongs at the request boundary.

Hermes' credential-pool docs also warn that switching credentials during a long-lived conversation can lose provider-side cached prefixes. A deterministic once-per-turn preference minimizes unnecessary switching.

---

## Minimal credential-pool API support

The hook needs a safe way for Hermes core to select one exact pool entry without changing the configured strategy.

Make `CredentialPool.select()` accept one optional keyword-only argument:

```python
def select(self, *, credential_id: str | None = None) -> PooledCredential | None:
```

Behavior:

```text
credential_id is None
    -> EXACT existing select() behavior

credential_id is provided
    -> return that specific credential only if it is currently available
    -> preserve existing OAuth refresh handling for that entry
    -> return None if it cannot be used
    -> do NOT silently choose a different entry
```

The exact-selection path should reuse `_available_entries(clear_expired=True, refresh=True)` so Hermes remains the authority on cooldown, DEAD/EXHAUSTED state, stale auth-store synchronization, and OAuth refresh.

Important: if the requested entry is in the deferred single-use-token refresh list (Codex/xAI OAuth), perform the normal refresh outside the pool lock and then retry the **same requested ID**, not normal strategy selection.

Conceptually:

```python
def select(self, *, credential_id=None):
    entry, pending = self._select_under_lock(credential_id=credential_id)
    if pending:
        self._refresh_pending_entries(pending)
    if entry is not None:
        return entry
    if pending:
        entry, _ = self._select_under_lock(credential_id=credential_id)
    return entry
```

and in `_select_unlocked(...)`:

```python
available, pending = self._available_entries(clear_expired=True, refresh=refresh)

if credential_id is not None:
    entry = next((e for e in available if e.id == credential_id), None)
    if entry is not None:
        self._current_id = entry.id
    return entry, pending

# existing random / least_used / round_robin / fill_first code unchanged
```

Do not alter any existing no-argument call sites.

---

## Turn integration point

Current Hermes turn setup lives in `agent/turn_context.py`. It begins each turn by restoring the primary runtime with:

```python
agent._restore_primary_runtime()
```

The new selector should run immediately after primary restoration and before Hermes binds the live main runtime into `auxiliary_client.set_runtime_main(...)`.

Reason:

- if the previous turn was on a fallback provider, primary restoration must happen first;
- the plugin should choose only from the restored primary provider's credential pool;
- auxiliary tasks for this turn should see the credential Hermes actually selected;
- this occurs before any provider request.

Suggested helper shape (exact naming may follow maintainer preference):

```python
def _apply_plugin_credential_preference(agent) -> None:
    pool = getattr(agent, "_credential_pool", None)
    if pool is None:
        return

    from hermes_cli.lifecycle import has_hook, invoke_hook
    if not has_hook("pre_credential_select"):
        return

    results = invoke_hook(
        "pre_credential_select",
        session_id=agent.session_id or "",
        provider=agent.provider or "",
        model=agent.model or "",
        platform=getattr(agent, "platform", None) or "",
        current_credential_id=getattr(agent, "_credential_pool_entry_id", None),
    )

    preferred_id = first_valid_credential_id(results)
    if not preferred_id:
        return

    entry = pool.select(credential_id=preferred_id)
    if entry is None:
        return

    runtime_key = entry.runtime_api_key or entry.access_token
    runtime_base = entry.runtime_base_url or entry.base_url or agent.base_url
    already_live = (
        getattr(agent, "_credential_pool_entry_id", None) == entry.id
        and getattr(agent, "api_key", None) == runtime_key
        and normalize(agent.base_url) == normalize(runtime_base)
    )
    if not already_live:
        agent._swap_credential(entry)
```

The actual implementation should reuse existing URL normalization helpers rather than introduce a new normalizer.

### Do not rebuild the client unnecessarily

If the chosen entry is already the live credential and its runtime token/base URL have not changed, do nothing. This avoids pointless client reconstruction and helps preserve connection/prompt-cache behavior.

If the same credential ID was refreshed and its access token changed, `_swap_credential(entry)` must still run so the live client gets the refreshed token.

---

## Hook registration

In `hermes_cli/plugins.py`:

```python
VALID_HOOKS = {
    ...,
    "pre_credential_select",
    ...,
}
```

This is a directive/control hook. Hermes' shell-hook parser does not currently have a response shape for this directive, so also add it to:

```python
SHELL_UNSUPPORTED_HOOKS = {
    "transform_api_error_classification",
    "pre_credential_select",
}
```

That prevents shell hooks from registering successfully and then having their credential directive silently ignored.

Do not add a new capability flag or config key unless maintainers explicitly request it. Enabled Python plugins already execute in-process and existing directive hooks can modify host behavior; adding a third permission mechanism is YAGNI for this PR.

---

# Upstream change 2: credential identity on existing API hooks

## Contract

Add these two additive keyword fields to the existing request-scoped hooks:

```text
credential_id
credential_label
```

Hooks:

```text
pre_api_request
post_api_request
api_request_error
```

Values:

```python
credential_id: str | None
credential_label: str | None
```

They must identify the credential that actually dispatched that provider request.

No secret values are exposed.

Why all three hooks:

- `pre_api_request`: lets a plugin display which account is about to be used;
- `post_api_request`: lets telemetry/status consumers attribute success to the same account;
- `api_request_error`: lets consumers attribute a failure to the exact account before Hermes rotates it.

Current Hermes already maintains `agent._credential_pool_entry_id` specifically to keep stable attribution when runtime tokens rotate or the pool cursor moves. Reuse that identity rather than relying on `pool.current()`.

---

## One tiny identity helper

Avoid repeating label lookup at three call sites.

Add a small helper on `AIAgent` or in `agent/agent_runtime_helpers.py`, whichever produces the smaller clean diff:

```python
def _credential_identity_for_hook(self):
    credential_id = getattr(self, "_credential_pool_entry_id", None)
    if not credential_id:
        return None, None

    pool = getattr(self, "_credential_pool", None)
    if pool is None:
        return credential_id, None

    try:
        entry = next((e for e in pool.entries() if e.id == credential_id), None)
    except Exception:
        entry = None

    return credential_id, getattr(entry, "label", None) if entry else None
```

Only call this helper inside existing `has_hook(...)` gates so the no-plugin hot path stays cheap.

Then pass:

```python
credential_id=_credential_id,
credential_label=_credential_label,
```

into all three hooks.

Do not expose:

- access token;
- refresh token;
- runtime API key;
- credential object;
- auth-store path.

---

# Files expected to change upstream

Keep the PR limited to roughly these files:

```text
agent/credential_pool.py
agent/turn_context.py
run_agent.py                         # or a runtime helper for identity lookup
hermes_cli/plugins.py
website/docs/user-guide/features/hooks.md
tests/agent/test_credential_pool.py
tests/agent/test_turn_context.py
tests/run_agent/test_run_agent.py
```

Potentially one existing plugin-compat test file if needed by maintainer expectations:

```text
tests/hermes_cli/test_plugin_api_compat.py
```

Avoid touching unrelated provider, auth, CLI, gateway, or Codex code.

---

# Required tests

## 1. Exact credential selection behavior

Add behavior tests in `tests/agent/test_credential_pool.py`.

Required cases:

### Requested available ID wins

Given two healthy entries and a strategy that would normally choose A:

```python
assert pool.select(credential_id="B").id == "B"
```

### Unknown requested ID does not silently route elsewhere

```python
assert pool.select(credential_id="missing") is None
```

### Exhausted/dead requested ID is rejected

Requested unavailable entry returns `None` even if another healthy credential exists.

### Existing no-argument strategies are unchanged

Existing tests for `fill_first`, `least_used`, `round_robin`, and `random` must continue passing unchanged.

### Deferred OAuth refresh preserves requested identity

If a requested Codex/xAI OAuth entry needs deferred refresh, refresh it and retry the same ID. Do not fall through to normal selection after refresh.

This is an important invariant because single-use refresh handling is explicitly designed to happen outside the pool lock.

---

## 2. Hook runs once per turn and swaps safely

Add focused tests in `tests/agent/test_turn_context.py`.

Required cases:

- no registered `pre_credential_select` hook -> zero behavior change;
- hook returns `None` -> zero behavior change;
- hook returns malformed result -> ignored;
- hook raises -> turn proceeds with existing credential;
- hook returns available ID -> `_swap_credential()` is called when necessary;
- hook chooses already-live unchanged credential -> no unnecessary swap/client rebuild;
- hook chooses same ID whose refreshed token differs -> swap occurs;
- hook returns unavailable ID -> no swap, no crash, Hermes keeps existing runtime.

Do not test by reading source text. Exercise the real Python path.

---

## 3. API hooks report actual credential identity

Extend the existing request-hook tests in `tests/run_agent/test_run_agent.py`.

For `pre_api_request` and `post_api_request`, assert:

```python
payload["credential_id"] == expected_id
payload["credential_label"] == expected_label
```

For `api_request_error`, assert the same identity is emitted **before** recovery rotates to a different entry.

Also cover an agent with no credential pool:

```python
credential_id is None
credential_label is None
```

This preserves a consistent payload shape for non-pooled providers.

---

## 4. Plugin compatibility behavior

Because this becomes documented plugin surface, add or extend a PluginManager behavior test that registers:

```python
ctx.register_hook("pre_credential_select", callback)
```

and proves the callback loads/invokes through the real plugin dispatcher.

Callbacks in docs/tests should accept `**kwargs`, matching Hermes' forward-compatibility guidance.

---

# Documentation change

Update `website/docs/user-guide/features/hooks.md` only as much as required.

Add to the hook catalog:

```text
pre_credential_select | Directive/control | Once per user turn after primary runtime restore and before provider requests. First valid {"credential_id": "..."} asks Hermes to use that currently available pool entry. | session_id, provider, model, platform, current_credential_id | No secrets; credential IDs are opaque.
```

Document these additive fields for:

```text
pre_api_request
post_api_request
api_request_error
```

```text
credential_id: opaque pool entry ID or null
credential_label: human-readable pool label or null
```

Explicitly state:

- selecting a credential is a preference/request, not ownership of failover;
- Hermes can still rotate after runtime failures;
- plugins should avoid switching credentials unnecessarily because provider prompt caching can be credential-sensitive;
- no credential secrets are exposed by the hooks.

No config documentation is needed because this adds no user-facing Hermes setting.

---

# External plugin behavior after upstream merge

This is **not part of the upstream PR**, but it is the concrete consumer proving the hook is non-speculative.

`NotRllyRn/hermes-codex-pool` would add:

```yaml
provides_hooks:
  - pre_credential_select
  - pre_api_request
```

## Selection policy

On `pre_credential_select` for `provider == "openai-codex"`:

1. load the native Codex pool;
2. consider only Hermes-healthy entries;
3. fetch each candidate's native Hermes account-usage snapshot;
4. identify the Weekly window;
5. choose the account with the earliest valid Weekly `reset_at`;
6. use stable pool order/ID as the tie-breaker;
7. return `{"credential_id": chosen.id}`;
8. on any inability to compute a choice, return `None` and let stock Hermes continue.

Do not implement refresh, retries, exhaustion marking, or failover in the plugin.

## User-visible selection line

On the first `pre_api_request` for each `turn_id`, show one concise line using the **actual** identity Hermes reports:

```text
[Codex: work · Weekly 38% left · resets in 18h · earliest weekly reset]
```

Deduplicate by `turn_id` so tool-loop API calls do not spam the user.

If Hermes rejected the plugin's requested credential and used another one, the displayed account must come from `pre_api_request.credential_id` / `credential_label`, not from the plugin's intended choice.

That makes the UI truthful even when cooldowns, refresh failures, or races change the actual route.

---

# What must NOT be included in the upstream PR

Do not add any of the following:

- `smart-reset` strategy to Hermes core;
- Codex-specific quota sorting;
- OpenAI usage endpoint calls;
- token import UI;
- new CLI commands;
- new model tools;
- background workers;
- caching subsystem;
- new database/state files;
- `HERMES_*` behavior environment variables;
- changes to Hermes' existing failover policy;
- special references to `hermes-codex-pool` in runtime code;
- raw credential/token data in hook payloads.

The PR should be defensible without knowing what Codex is: “allow a trusted plugin to request one available credential-pool entry at a turn boundary and observe the non-secret identity actually used.”

---

# Upstream workflow

## 1. Rebase/start from current `main`

Do not implement directly against the August 18 snapshot without first checking current upstream `main`.

```bash
git fetch upstream
git checkout main
git pull --ff-only upstream main
git checkout -b feat/plugin-credential-selection
```

Hermes moves quickly; re-run searches for these symbols before editing:

```bash
rg -n "VALID_HOOKS|SHELL_UNSUPPORTED_HOOKS" hermes_cli/plugins.py
rg -n "def select\(" agent/credential_pool.py
rg -n "_restore_primary_runtime|set_runtime_main" agent/turn_context.py
rg -n "pre_api_request|post_api_request|api_request_error" agent run_agent.py
```

## 2. Search issues/PRs again before coding

Repository policy explicitly asks contributors to search both issues and PRs first.

```bash
gh search issues --repo NousResearch/hermes-agent 'credential selection plugin'
gh search issues --repo NousResearch/hermes-agent 'credential_id pre_api_request'
gh search prs --repo NousResearch/hermes-agent 'credential selection plugin'
gh search prs --repo NousResearch/hermes-agent 'credential pool hook'
```

If an overlapping open PR exists, do not open a competing implementation.

## 3. Open a small feature issue first

A permanent directive hook deserves maintainer visibility before code even though the implementation is small.

Suggested title:

```text
Feature: plugin hook for per-turn credential preference and request credential identity
```

Suggested issue body:

```markdown
## Problem

A standalone plugin can manage/inspect Hermes credential-pool entries, but there is no supported way to request one healthy pool entry at a user-turn boundary or observe which pool entry actually dispatched an API request.

Concrete consumer: https://github.com/NotRllyRn/hermes-codex-pool

That plugin manages multiple OpenAI Codex OAuth entries and can read their per-account quota windows. It wants to prefer the healthy account whose Weekly window resets soonest while leaving OAuth refresh, cooldowns, retries, and failover entirely owned by Hermes.

Today this requires monkey-patching/private runtime access.

## Proposed generic surface

1. Add a once-per-turn `pre_credential_select` plugin hook. A callback may return `{"credential_id": "<opaque pool id>"}`. Hermes validates the requested entry and performs the normal credential swap. Invalid/unavailable IDs are ignored; native failure rotation remains unchanged.

2. Add `credential_id` and `credential_label` keyword fields to `pre_api_request`, `post_api_request`, and `api_request_error`, using Hermes' existing `_credential_pool_entry_id` attribution. No secrets are exposed.

No provider-specific selection strategy or third-party plugin code would be added to core.

## Why a hook

AGENTS.md says that when a real external plugin needs a capability the framework does not expose, the generic plugin surface should be widened rather than having the plugin modify/patch core. This is a concrete external consumer, so the hook is not speculative.

## Compatibility

- no-hook path unchanged;
- `CredentialPool.select()` keeps identical no-argument behavior;
- hook payload additions are keyword-only/additive;
- failures are fail-open;
- no new config/env vars or model tools.
```

If maintainers prefer a different hook name or want the selection directive folded into another existing plugin control surface, follow that preference before coding.

## 4. Implement as one focused PR

These two changes are one logical capability: credential-aware plugins. Do not mix unrelated cleanup/refactors.

Suggested PR title:

```text
feat(plugins): expose credential preference and request identity
```

Suggested commit structure (one commit is also fine):

```text
feat(plugins): add per-turn credential selection hook
feat(plugins): expose request credential identity

docs(plugins): document credential selection hook
```

Conventional Commits are required by the repo guide.

## 5. Test with Hermes' required runner

Targeted first:

```bash
scripts/run_tests.sh tests/agent/test_credential_pool.py
scripts/run_tests.sh tests/agent/test_turn_context.py
scripts/run_tests.sh tests/run_agent/test_run_agent.py
scripts/run_tests.sh tests/hermes_cli/test_plugins.py
```

Then full suite before PR:

```bash
scripts/run_tests.sh
```

Also run Hermes manually with a tiny local test plugin that returns a known available credential ID and logs the API-hook identity.

Do not use live OpenAI credentials in automated tests.

## 6. PR description must include

Per `CONTRIBUTING.md` / the PR template:

- what changed and why;
- link to the feature issue;
- link to `NotRllyRn/hermes-codex-pool` as the concrete external consumer;
- exact files changed;
- how to test;
- platform(s) manually tested;
- confirmation no secrets are included in hook payloads;
- confirmation existing no-hook behavior and pool strategies remain unchanged.

Suggested opening paragraph:

```markdown
This widens Hermes' generic plugin surface for a concrete standalone-plugin use case without adding provider-specific routing to core. A plugin may request one available credential-pool entry once per user turn, while Hermes remains authoritative for validation, OAuth refresh, cooldowns, retry/failover, and client swapping. Existing API lifecycle hooks also receive the opaque credential ID/label actually used, enabling truthful per-turn status UI without exposing secrets.
```

---

# Review-sensitive details

These are likely to matter during upstream review.

## Preserve prompt-cache behavior

Do not select credentials per tool-loop API call. Fire once per user turn.

Do not call `_swap_credential()` when the same ID/token/base URL is already live.

## Fail open

A broken plugin must never prevent a normal Hermes turn. Hook errors, invalid return values, stale IDs, and unavailable IDs should all fall back to stock behavior.

## Hermes remains authoritative

The plugin requests an ID. Hermes decides whether that credential is usable. Never let a plugin directly inject an access token into the live agent through this hook.

## No secret observer payloads

Only stable ID + label. The existing sanitized `request` / `response` payload rules remain intact.

## Do not conflate preference with failover

Once the request is sent, the existing error classifier and `mark_exhausted_and_rotate` path must remain untouched.

## Do not modify strategy persistence

A plugin's per-turn selection must not rewrite `credential_pool.strategy`, priorities, request counts, or auth storage solely to express its preference.

## No new config

The external plugin owns its own selection-policy setting if it needs one. Hermes core only exposes the generic hook.

---

# Definition of done

The upstream PR is complete when all are true:

- [ ] `pre_credential_select` is a documented Python plugin directive hook.
- [ ] It fires once per user turn after primary runtime restoration and before the first provider request.
- [ ] It accepts only an opaque `credential_id` directive.
- [ ] Hermes validates the ID against its current credential pool.
- [ ] Exact selection reuses Hermes availability/cooldown/OAuth-refresh logic.
- [ ] Invalid/unavailable preference is fail-open and does not silently route to a different requested ID.
- [ ] Existing no-argument `CredentialPool.select()` semantics are unchanged.
- [ ] Existing native retry/rotation/failover remains unchanged.
- [ ] `pre_api_request`, `post_api_request`, and `api_request_error` include `credential_id` + `credential_label`.
- [ ] The identity corresponds to the credential that actually dispatched the request.
- [ ] No tokens/API keys/credential objects are exposed.
- [ ] No hook registered = negligible/no new hot-path work.
- [ ] Behavior tests cover exact selection, unavailable selection, refresh, once-per-turn invocation, and API-hook attribution.
- [ ] Docs are updated.
- [ ] `scripts/run_tests.sh` passes.
- [ ] Manual test confirms an external plugin can select an entry and observe the actual identity.
- [ ] PR contains no Codex-specific policy or external plugin source.

---

# Final architectural boundary

```text
hermes-codex-pool (external repo)
    |
    | pre_credential_select -> {credential_id}
    v
Hermes credential pool
    |- validates availability
    |- refreshes OAuth if needed
    |- swaps runtime credential
    |- keeps normal retry/failover behavior
    |
    | pre/post/error API hooks
    | + credential_id
    | + credential_label
    v
hermes-codex-pool UI
    -> "[Codex: work · Weekly 38% left · resets in 18h · earliest weekly reset]"
```

This is the intended YAGNI boundary: **policy and UI in the plugin; credential authority and transport in Hermes.**
