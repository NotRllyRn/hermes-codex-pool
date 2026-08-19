# Hermes Codex Pool — Implementation Plan

**Working repository name:** `hermes-codex-pool`  
**Plugin name:** `hermes-codex-pool`  
**Primary CLI:** `hermes codex-pool`  
**Read-only in-session command:** `/codex-pool`  
**Target:** Hermes Agent `0.20.0` and current `main` as researched on **2026-08-11**  
**Design principle:** YAGNI — manage credentials and display status; let Hermes own routing, refresh, retry, cooldown, and failover.

> **Implementation status:** v0.1.0 code complete on 2026-08-19. The API boundary was rechecked against Hermes 0.20.4/main `395c70d6`; see `COMPATIBILITY.md`. Live-account acceptance checks remain release-time manual tests because they require real OAuth credentials.

---

## 1. Executive decision

Build this as a **standalone native Hermes general plugin in its own GitHub repository**. It does **not** require a Hermes fork or a commit to `NousResearch/hermes-agent`.

The plugin should do only five things:

1. **Add/import** a Codex OAuth access token + refresh token pair into Hermes' native `openai-codex` credential pool.
2. **List** the Codex pool entries Hermes currently knows about.
3. **Rename** an entry's human-readable label.
4. **Remove** an entry.
5. **Show status/usage** for each account using Hermes' existing Codex usage client.

Everything else remains Hermes' job:

- selecting a credential;
- `fill_first`, `round_robin`, `least_used`, or `random` policy;
- refreshing OAuth access tokens;
- rotating refresh tokens;
- marking credentials exhausted/dead;
- honoring quota reset times/cooldowns;
- retrying requests;
- switching to another healthy credential;
- cross-provider fallback.

This is deliberately **not a second pi-relay runtime**. It is a thin management/UI layer over Hermes' existing credential pool.

---

## 2. Research findings that drive the design

### 2.1 Hermes explicitly supports standalone plugins

The official Hermes plugin guide says third-party plugins should ship as standalone repositories and can be installed under `~/.hermes/plugins/`. A native plugin needs `plugin.yaml`, `__init__.py`, and a `register(ctx)` function.

Normal install/enable flow:

```bash
hermes plugins install OWNER/hermes-codex-pool --no-enable
hermes plugins enable hermes-codex-pool
```

The plugin system exposes both:

- `ctx.register_cli_command(...)` → terminal command trees such as `hermes codex-pool add`;
- `ctx.register_command(...)` → in-session slash commands such as `/codex-pool` that work in CLI and gateway sessions.

**Design consequence:** token mutation belongs in a terminal-only CLI command. The slash command should be read-only because slash commands can also be invoked through gateway surfaces such as Telegram/Discord.

Official documentation:

- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- CLI registration section: `register_cli_command`
- Slash command section: `register_command`

Current upstream implementation:

- `hermes_cli/plugins.py` — `PluginContext.register_cli_command`
- `hermes_cli/plugins.py` — `PluginContext.register_command`

Uploaded Hermes `0.20.0` snapshot:

- `hermes_cli/plugins.py:552` — `register_cli_command`
- `hermes_cli/plugins.py:577` — `register_command`

### 2.2 Hermes already has the runtime behavior pi-relay would otherwise need to implement

Hermes' official credential-pool documentation states that it can hold multiple credentials for one provider and rotate when a credential reaches a rate/usage/billing limit or authentication refresh fails.

Current strategies are:

- `fill_first` — default; use first healthy credential until unavailable;
- `round_robin`;
- `least_used`;
- `random`.

Official documentation:

- https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools

Current upstream implementation:

- `agent/credential_pool.py`
- `PooledCredential`
- `CredentialPool`
- `load_pool(provider)`
- `CredentialPool.add_entry(entry)`
- `CredentialPool.remove_index(index)`
- `CredentialPool.resolve_target(target)`
- `get_pool_strategy(provider)`

**Design consequence:** this plugin must not intercept LLM requests or duplicate credential rotation. That would create two competing sources of routing truth.

### 2.3 Hermes' own Codex add flow shows the exact credential shape we should reproduce

Current Hermes `auth_commands.py` creates each Codex account as a distinct native pool entry using:

```python
PooledCredential(
    provider="openai-codex",
    id=uuid.uuid4().hex[:6],
    label=label,
    auth_type=AUTH_TYPE_OAUTH,
    priority=0,
    source=SOURCE_MANUAL_DEVICE_CODE,
    access_token=...,
    refresh_token=...,
    base_url=...,
    last_refresh=...,
)
```

Then it calls:

```python
pool.add_entry(entry)
```

For the first credential, Hermes calls:

```python
auth_mod.mark_provider_active_if_unset("openai-codex")
```

This is important because `manual:device_code` entries are treated as independent/self-contained OAuth credentials and refresh from their own token pair. That is exactly the behavior needed for manually pasted accounts.

Current upstream source:

- `hermes_cli/auth_commands.py`, current Codex branch around lines 286–321 in the researched source.
- `agent/credential_pool.py`, `SOURCE_MANUAL_DEVICE_CODE = "manual:device_code"`.

Uploaded Hermes `0.20.0` snapshot:

- `hermes_cli/auth_commands.py:310+` — Codex branch
- `agent/credential_pool.py:186+` — `PooledCredential`
- `agent/credential_pool.py:2383+` — `add_entry`

### 2.4 Hermes already has a per-account Codex usage client

`agent.account_usage.fetch_account_usage(...)` accepts explicit `base_url` and `api_key` arguments. For `openai-codex`, Hermes queries the Codex usage endpoint and parses:

- `primary_window` → **Session**;
- `secondary_window` → **Weekly**;
- percentage used;
- reset time;
- optional reset-credit/credit information.

`render_account_usage_lines(...)` already converts that snapshot into readable lines including remaining percentage and reset timing.

Current upstream source:

- `agent/account_usage.py` — `_fetch_codex_account_usage`
- `agent/account_usage.py` — `fetch_account_usage`
- `agent/account_usage.py` — `render_account_usage_lines`

Uploaded Hermes `0.20.0` snapshot:

- `agent/account_usage.py:510+` — Codex usage fetch
- `agent/account_usage.py:884+` — `fetch_account_usage`
- `agent/account_usage.py:95+` — renderer

**Design consequence:** do not copy pi-relay's HTTP usage client. Reuse Hermes' implementation so endpoint/path changes remain Hermes' responsibility.

### 2.5 Hermes' auth store writer is concurrency-aware

Current `hermes_cli.auth.write_credential_pool(...)` persists pool entries under Hermes' auth-store lock. It re-reads the disk state and merges concurrent additions/status changes, and accepts `removed_ids` so an intentionally removed credential is not resurrected by the merge.

**Design consequence:** never manually `json.load()` / `json.dump()` `~/.hermes/auth.json` from the plugin. Use Hermes' pool/writer APIs.

### 2.6 Refresh tokens require one owner

Current Hermes source explicitly handles rotating/single-use OAuth refresh-token hazards across processes/profiles. The plugin should therefore **never implement its own refresh loop** and should not continuously synchronize the same refresh-token pair with another client.

**Design consequence:** importing a token pair means Hermes becomes the runtime owner of that imported pair. The plugin only inserts/manages the row.

---

## 3. What to borrow from pi-relay — and what not to borrow

The uploaded pi-relay has a useful account-management UX:

- one dashboard-like command;
- labeled accounts;
- per-account short/long quota status;
- add/rename/delete operations;
- clear status presentation.

Those ideas should carry over.

Do **not** port these pi-relay responsibilities because Hermes already owns them:

- custom `smart-reset` / `most-available` selection;
- provider request interception;
- stream replay/continuation handling;
- token refresh orchestration;
- quota-wait background logic;
- a separate credential vault/state JSON;
- file locking for a plugin-owned vault;
- request-level failover;
- account pinning.

The Hermes plugin should look similar to pi-relay at the **management/status layer**, not at the runtime-routing layer.

---

## 4. User experience

### 4.1 Install

```bash
hermes plugins install YOUR_GITHUB_USER/hermes-codex-pool --enable
```

If `--enable` is unavailable in a particular Hermes build, the documented two-step flow is:

```bash
hermes plugins install YOUR_GITHUB_USER/hermes-codex-pool --no-enable
hermes plugins enable hermes-codex-pool
```

### 4.2 Default dashboard

```bash
hermes codex-pool
```

Equivalent to:

```bash
hermes codex-pool status
```

Suggested output:

```text
OpenAI Codex credential pool · strategy: fill_first

#1  work        available   Session 72% left · resets in 2h 14m
                           Weekly  38% left · resets in 4d 8h
#2  personal    exhausted   Session  0% left · resets in 31m
                           Weekly  81% left · resets in 5d 2h
#3  backup      available   usage unavailable

Hermes owns selection, refresh, and automatic failover.
```

Important wording:

- use `available`, `exhausted`, or `dead` based on Hermes' persisted pool state;
- do **not** label an account `active` unless the plugin has a reliable runtime context proving that; a freshly loaded pool's `peek()` is not necessarily the credential currently used by another Hermes process;
- optionally show `first eligible` rather than `active` if useful;
- do not print tokens, partial tokens, refresh-token fingerprints, or raw auth errors containing secrets.

### 4.3 Add/import

```bash
hermes codex-pool add
```

Interactive flow:

```text
Label [openai-codex-oauth-2]: work
Access token:  ********
Refresh token: ********
Added "work" to Hermes' openai-codex credential pool.
```

Optional label flag:

```bash
hermes codex-pool add --label work
```

**Do not support `--access-token` or `--refresh-token` flags.** Tokens in command arguments can leak into shell history, process listings, logs, terminal scrollback, or automation traces. Always use Hermes' `masked_secret_prompt`.

### 4.4 List without network requests

```bash
hermes codex-pool list
```

Suggested output:

```text
#1  4b29ad  work       oauth  available
#2  70c118  personal   oauth  exhausted
#3  9e44c1  backup     oauth  available
```

This should be fast and local-only. It is useful when the usage API is slow/unreachable.

### 4.5 Status for one entry

Targets should follow Hermes' native targeting behavior: ID, unique label, or 1-based index.

```bash
hermes codex-pool status work
hermes codex-pool status 4b29ad
hermes codex-pool status 1
```

Use `CredentialPool.resolve_target()` rather than reimplementing target parsing.

### 4.6 Rename

```bash
hermes codex-pool rename work work-main
```

Rules:

- label must be non-empty after trimming;
- labels should preferably be unique case-insensitively;
- reject a rename that creates an ambiguous duplicate label;
- preserve ID, priority, tokens, refresh metadata, status, error/reset metadata, base URL, and request count.

### 4.7 Remove

```bash
hermes codex-pool remove work
```

Interactive terminal confirmation:

```text
Remove Codex credential "work" (4b29ad)? [y/N]:
```

Automation escape hatch:

```bash
hermes codex-pool remove work --yes
```

Use `CredentialPool.resolve_target()` and `CredentialPool.remove_index()`.

### 4.8 In-session status command

```text
/codex-pool
```

or:

```text
/codex-pool status
```

This command is **read-only**. It may return the same compact status dashboard.

Do not implement:

```text
/codex-pool add
/codex-pool remove
/codex-pool rename
```

because Hermes slash commands also work through gateway sessions. Keeping secrets and destructive credential operations terminal-only gives the plugin a simpler and safer boundary.

---

## 5. Command surface — final v1

```text
hermes codex-pool
hermes codex-pool list
hermes codex-pool status [TARGET]
hermes codex-pool add [--label LABEL]
hermes codex-pool rename TARGET NEW_LABEL
hermes codex-pool remove TARGET [--yes]

/codex-pool
/codex-pool status
/codex-pool help
```

That is all for v1.

### Explicitly excluded from v1

- enable/disable account flag;
- reorder/prioritize accounts;
- change Hermes rotation strategy;
- pin an account;
- force token refresh;
- reset quota cooldowns;
- redeem Codex reset credits;
- copy/export credentials;
- import from arbitrary files;
- background usage polling;
- notifications;
- GUI/TUI;
- local web server;
- plugin-owned config/state file;
- model-callable tools;
- middleware;
- provider replacement;
- custom failover logic;
- automatic quota-aware selection.

Most of these are either Hermes responsibilities or unnecessary until a real user need appears.

---

## 6. Architecture

### 6.1 Source of truth

There must be exactly one credential source of truth:

```text
~/.hermes/auth.json
    credential_pool
        openai-codex
            [Hermes PooledCredential rows]
```

The plugin does not own another database.

### 6.2 Data flow: import

```text
User
  │
  │ hermes codex-pool add
  ▼
Plugin CLI
  │
  ├─ masked_secret_prompt(access token)
  ├─ masked_secret_prompt(refresh token)
  │
  ▼
PooledCredential(... auth_type="oauth", source="manual:device_code" ...)
  │
  ▼
load_pool("openai-codex").add_entry(entry)
  │
  ▼
Hermes write_credential_pool / auth-store lock
  │
  ▼
~/.hermes/auth.json
  │
  └─ Hermes runtime later selects/refreshes/rotates normally
```

### 6.3 Data flow: status

```text
load_pool("openai-codex")
  │
  ├─ entries() → labels, ids, Hermes local status
  │
  └─ for each entry:
       fetch_account_usage(
           "openai-codex",
           base_url=entry.runtime_base_url,
           api_key=entry.runtime_api_key,
       )
             │
             ▼
       Hermes Codex usage client
             │
             ▼
       Session / Weekly windows + reset times
```

Passing the entry's explicit `api_key` is essential: otherwise `fetch_account_usage()` may resolve/select some other credential from the pool, which would make the displayed row inaccurate.

### 6.4 Data flow: rename

There is no public `CredentialPool.rename()` method today.

Recommended implementation:

1. `pool = load_pool(PROVIDER)`
2. `index, entry, error = pool.resolve_target(target)`
3. `entries = pool.entries()`
4. replace exactly that row with `dataclasses.replace(entry, label=new_label)`
5. call `hermes_cli.auth.write_credential_pool(PROVIDER, [e.to_dict() for e in entries])`

Why this path:

- it preserves every other field;
- it uses Hermes' concurrency-aware writer;
- it does not hand-edit JSON;
- it avoids calling private `CredentialPool._replace_entry()` / `_persist()` methods.

This still uses an internal Hermes auth API. See the compatibility section below.

### 6.5 Data flow: remove

```text
load_pool(PROVIDER)
  → resolve_target(target)
  → remove_index(index)
  → Hermes persists remaining entries with removed_ids
```

Use the pool's public operation rather than calling `write_credential_pool` directly for removal.

---

## 7. Hermes APIs/functions the plugin should use

### Stable/plugin-facing surface

From `PluginContext`:

```python
ctx.register_cli_command(...)
ctx.register_command(...)
```

These are the actual extension APIs.

### Hermes credential/runtime surface

From `agent.credential_pool`:

```python
AUTH_TYPE_OAUTH
SOURCE_MANUAL_DEVICE_CODE
STATUS_EXHAUSTED
STATUS_DEAD
PooledCredential
get_pool_strategy
load_pool
```

From the returned `CredentialPool`:

```python
pool.entries()
pool.resolve_target(target)
pool.add_entry(entry)
pool.remove_index(index)
```

From `hermes_cli.auth`:

```python
DEFAULT_CODEX_BASE_URL
mark_provider_active_if_unset
write_credential_pool
```

From `hermes_cli.secret_prompt`:

```python
masked_secret_prompt
```

From `agent.account_usage`:

```python
fetch_account_usage
render_account_usage_lines
```

### APIs explicitly not to use

Avoid:

```python
pool.select()                  # changes/participates in selection; status should be observational
pool._replace_entry(...)       # private
pool._persist(...)             # private
pool._refresh_entry(...)       # private
pool.try_refresh_current(...)  # plugin should not own refreshing
```

Avoid direct access to:

```text
~/.hermes/auth.json
```

except through Hermes APIs.

---

## 8. Minimal repository layout

```text
hermes-codex-pool/
├── plugin.yaml
├── __init__.py
├── codex_pool.py
├── README.md
├── IMPLEMENTATION.md
├── LICENSE
├── .gitignore
├── pyproject.toml
└── tests/
    └── test_codex_pool.py
```

This is intentionally small.

### Why there is no `schemas.py` or `tools.py`

The plugin exposes **no LLM-callable tools**. It is an operator credential manager. Adding a model tool that can mutate authentication state would increase attack surface for no useful v1 benefit.

### Why there is no `state.json`

Hermes already owns the credential pool. A second store creates synchronization problems and makes OAuth refresh-token rotation harder to reason about.

### Why there is no `usage.py`

Hermes already implements Codex usage fetching/parsing.

### Why there is no `router.py`

Hermes already implements selection/failover.

### Why keep `pyproject.toml`

A Git-installed directory plugin does not strictly need it, but it is useful for:

- `pytest` development;
- package metadata;
- optional future pip distribution through the official `hermes_agent.plugins` entry-point mechanism.

Keep it minimal.

---

## 9. File-by-file design

## `plugin.yaml`

Purpose: Hermes discovery metadata only.

Recommended contents:

```yaml
name: hermes-codex-pool
version: "0.1.0"
description: Manage OpenAI Codex OAuth credentials in Hermes and inspect per-account usage.
author: YOUR_NAME
```

Do not declare `provides_tools` because none are registered.

Do not declare token `requires_env`; tokens are stored by Hermes after the user explicitly imports them.

---

## `__init__.py`

Purpose: registration only. Keep this file boring.

Recommended shape:

```python
"""Hermes Codex Pool plugin registration."""

from .codex_pool import handle_cli, handle_slash, setup_cli


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
```

No global state. No threads. No startup I/O.

---

## `codex_pool.py`

Purpose: all v1 logic. One module is enough.

Suggested constants:

```python
PROVIDER = "openai-codex"
```

Suggested functions:

```python
def setup_cli(parser) -> None: ...
def handle_cli(args) -> None: ...
def handle_slash(raw_args: str) -> str: ...

def add_account(label: str | None = None) -> str: ...
def list_accounts() -> str: ...
def status_accounts(target: str | None = None) -> str: ...
def rename_account(target: str, new_label: str) -> str: ...
def remove_account(target: str, *, yes: bool = False) -> str: ...

def _resolve(pool, target): ...
def _local_state(entry) -> str: ...
def _format_usage(snapshot) -> list[str]: ...
def _default_label(pool) -> str: ...
def _label_exists(entries, label, *, exclude_id=None) -> bool: ...
```

Do not split these into more modules until the file becomes genuinely difficult to navigate.

### `setup_cli(parser)`

Build one argparse subcommand tree:

```text
add
list
status
rename
remove
```

Recommended parser shape:

```python
subs = parser.add_subparsers(dest="codex_pool_action")

add = subs.add_parser("add", help="Import an OAuth token pair")
add.add_argument("--label")

subs.add_parser("list", help="List local credential entries")

status = subs.add_parser("status", help="Show usage/status")
status.add_argument("target", nargs="?")

rename = subs.add_parser("rename", help="Rename an entry")
rename.add_argument("target")
rename.add_argument("new_label")

remove = subs.add_parser("remove", help="Remove an entry")
remove.add_argument("target")
remove.add_argument("--yes", action="store_true")
```

No extra parser options.

### `handle_cli(args)`

Dispatch with a simple `if/elif` chain. No command pattern/framework needed.

If no action is supplied, run `status_accounts()`.

### `add_account()`

Algorithm:

```text
pool = load_pool(PROVIDER)
label = provided label or prompt/default
access = masked_secret_prompt("Access token: ").strip()
refresh = masked_secret_prompt("Refresh token: ").strip()
reject if either empty
reject duplicate label
optionally reject exact duplicate access/refresh pair
first = not pool.entries()
entry = PooledCredential(... exact Hermes-compatible fields ...)
pool.add_entry(entry)
if first: mark_provider_active_if_unset(PROVIDER)
return success message
```

Recommended entry:

```python
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
```

Notes:

- `pool.add_entry()` assigns the real next priority; the initial `priority=0` mirrors Hermes' own add flow.
- Use `DEFAULT_CODEX_BASE_URL` rather than hard-coding the URL in the plugin.
- Do not make a refresh-token POST as validation. Consuming/rotating refresh tokens during import creates unnecessary state transitions.
- A best-effort usage check can be shown **after** successful import if desired, but v1 does not need it. The next `status` command is enough.
- Exact duplicate pair detection is safe and simple. Do not build JWT-account identity inference unless a real need appears.

### `list_accounts()`

Algorithm:

```text
entries = load_pool(PROVIDER).entries()
if empty: print clear setup message
for index, entry:
    print index, id, label, auth_type, local state
print current Hermes strategy
```

This command must make **zero network requests**.

Local state mapping:

```python
if entry.last_status == STATUS_DEAD:
    return "dead"
if entry.last_status == STATUS_EXHAUSTED:
    return "exhausted"
return "available"
```

Do not over-engineer cooldown calculations. Hermes already stores reset metadata and owns actual eligibility logic.

### `status_accounts()`

Algorithm:

```text
pool = load_pool(PROVIDER)
entries = pool.entries()
if target: resolve exactly one entry
for each selected entry:
    local_state = ...
    snapshot = fetch_account_usage(
        PROVIDER,
        base_url=entry.runtime_base_url or DEFAULT_CODEX_BASE_URL,
        api_key=entry.runtime_api_key,
    )
    render Session + Weekly usage
```

Important:

- **Do not call `pool.select()`** to fetch usage.
- Pass each entry's access token explicitly so each row queries the intended account.
- `fetch_account_usage()` is fail-open and may return `None`; render `usage unavailable` rather than treating this as exhaustion.
- Do not change pool status based solely on a diagnostic usage failure. Runtime Hermes owns health decisions.

For a clean pi-relay-like display, it is reasonable to format the returned snapshot yourself from its public fields rather than printing Hermes' full `render_account_usage_lines()` block per account. But use Hermes' snapshot parser/fetcher, not a copied endpoint client.

Minimal formatting helper:

```text
Session 72% left · resets in 2h 14m
Weekly  38% left · resets in 4d 8h
```

If maintaining reset formatting yourself becomes more than a few lines, call `render_account_usage_lines(snapshot)` instead. Simplicity wins.

### `rename_account()`

Algorithm:

```text
normalize new label
reject empty
pool = load_pool(PROVIDER)
resolve target with pool.resolve_target()
reject duplicate label excluding this entry
entries = pool.entries()
replace matching entry with dataclasses.replace(entry, label=new_label)
write_credential_pool(PROVIDER, [e.to_dict() for e in entries])
return success
```

No remove/re-add; that would unnecessarily change identity/order/status.

### `remove_account()`

Algorithm:

```text
pool = load_pool(PROVIDER)
index, entry, error = pool.resolve_target(target)
if error: return it
if not --yes: terminal confirmation
if confirmed: pool.remove_index(index)
return success
```

Never print tokens in the confirmation.

### `handle_slash()`

Accepted arguments:

```text
""
"status"
"help"
```

Behavior:

- `""` / `"status"` → return compact `status_accounts()` text;
- `"help"` → return `Usage: /codex-pool [status|help]`;
- anything else → return a message saying credential changes are terminal-only and show the `hermes codex-pool ...` command.

This is an intentional security boundary, not missing functionality.

---

## `README.md`

The README should be short and user-facing. Suggested complete draft:

```markdown
# hermes-codex-pool

Manage multiple OpenAI Codex OAuth accounts in Hermes Agent by pasting access + refresh tokens, and inspect each account's current Session/Weekly usage.

The plugin does **not** replace Hermes' credential router. Hermes continues to own OAuth refresh, cooldowns, retries, account selection, and automatic failover.

## Install

\`\`\`bash
hermes plugins install YOUR_GITHUB_USER/hermes-codex-pool --enable
\`\`\`

If needed:

\`\`\`bash
hermes plugins install YOUR_GITHUB_USER/hermes-codex-pool --no-enable
hermes plugins enable hermes-codex-pool
\`\`\`

## Usage

Show all Codex accounts and quota:

\`\`\`bash
hermes codex-pool
# or
hermes codex-pool status
\`\`\`

Import a token pair:

\`\`\`bash
hermes codex-pool add --label work
\`\`\`

The access and refresh tokens are requested through masked prompts and are written directly into Hermes' native \`openai-codex\` credential pool.

Other commands:

\`\`\`bash
hermes codex-pool list
hermes codex-pool status work
hermes codex-pool rename work work-main
hermes codex-pool remove work
\`\`\`

Inside a Hermes session:

\`\`\`text
/codex-pool
\`\`\`

The slash command is status-only. Credential mutations are terminal-only.

## What Hermes handles

Hermes already handles same-provider credential selection, OAuth refresh, cooldowns, retries, quota/rate-limit rotation, and fallback. This plugin intentionally does not duplicate those systems.

## Storage

There is no plugin-specific credential database. Credentials are stored by Hermes in its normal auth store (typically \`~/.hermes/auth.json\`) using Hermes' credential-pool APIs.

Do not share that file or paste its contents into bug reports.

## Compatibility

Initial target: Hermes Agent 0.20.0/current main as of 2026-08-11.

This plugin uses Hermes' official plugin API plus several internal credential-pool/auth helpers. If Hermes changes those internals, the plugin may need a compatibility update even when the plugin registration API remains stable.

## License

MIT
```

---

## `IMPLEMENTATION.md`

Use **this document** as the repository's `IMPLEMENTATION.md` during development. Once v1 ships, it can be shortened to architecture/maintenance notes, but keeping the design rationale is useful because the main risk is accidentally duplicating Hermes' credential runtime.

---

## `pyproject.toml`

Keep minimal:

```toml
[project]
name = "hermes-codex-pool"
version = "0.1.0"
description = "Manage OpenAI Codex OAuth pool entries for Hermes Agent."
requires-python = ">=3.11,<3.14"
dependencies = []

[project.entry-points."hermes_agent.plugins"]
hermes-codex-pool = "hermes_codex_pool"

[project.optional-dependencies]
dev = ["pytest"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

However, this introduces a packaging-layout decision: the Git directory plugin expects a root `__init__.py`, while a normal pip package should live in a package directory such as `hermes_codex_pool/`.

### YAGNI recommendation for v1

For the first release, **omit the pip entry point and treat GitHub installation as the supported distribution path**. The repository can still keep a tiny `pyproject.toml` for pytest tooling, or omit it entirely and use Hermes' own dev environment.

Simplest v1 `pyproject.toml`:

```toml
[project]
name = "hermes-codex-pool"
version = "0.1.0"
requires-python = ">=3.11,<3.14"
dependencies = []

[project.optional-dependencies]
dev = ["pytest"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

Do not solve pip packaging until there is demand for it.

---

## `.gitignore`

```gitignore
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
dist/
build/
*.egg-info/
```

Never put sample real tokens in fixtures.

---

## `LICENSE`

Use MIT if that matches the intended public release.

---

## `tests/test_codex_pool.py`

Keep one test module until it becomes unwieldy.

No real OpenAI network calls. No writes to the real `~/.hermes` directory.

### Required tests

#### Registration

- registers `codex-pool` CLI command;
- registers read-only `/codex-pool` slash command.

#### Add

- masked prompts are used for both tokens;
- empty access token fails;
- empty refresh token fails;
- generated credential has:
  - provider `openai-codex`;
  - `auth_type == oauth`;
  - `source == manual:device_code`;
  - provided label;
  - access token;
  - refresh token;
  - Codex base URL;
- calls `pool.add_entry` exactly once;
- first entry calls `mark_provider_active_if_unset`;
- later entries do not unnecessarily change active provider;
- neither token appears in stdout/errors/log messages.

#### List

- empty pool gives useful message;
- one/multiple rows format correctly;
- local states `available`, `exhausted`, and `dead` are rendered;
- no usage/network function is invoked.

#### Status

- each row calls `fetch_account_usage` with **that row's explicit token**;
- Session/Weekly data render correctly;
- `None` snapshot renders `usage unavailable`;
- one failed usage lookup does not prevent remaining accounts from rendering;
- status does not call `pool.select()`;
- status does not mutate/persist the pool.

#### Rename

- ID/label/index target resolution delegates to `resolve_target`;
- preserves every field except `label`;
- rejects blank label;
- rejects ambiguous duplicate label;
- uses Hermes' `write_credential_pool` rather than direct file I/O.

#### Remove

- target resolution delegates to Hermes;
- confirmation defaults to no;
- `--yes` skips confirmation;
- calls `remove_index` with correct 1-based index;
- output never contains token values.

#### Slash command

- `/codex-pool` shows status;
- `/codex-pool status` shows status;
- `/codex-pool help` shows help;
- mutation-like args are refused with terminal instructions;
- no slash path can call add/rename/remove.

### Test isolation

Prefer mocking imported Hermes functions inside `codex_pool.py` for unit tests. Add one lightweight integration test against a temporary `HERMES_HOME` if needed to confirm native persistence format.

Never run tests against the user's real Hermes home.

---

## 10. Security rules

These are requirements, not optional polish.

### Never echo credentials

Do not show:

- access tokens;
- refresh tokens;
- token prefixes;
- token suffixes;
- token hashes unless genuinely needed later.

Labels and Hermes-generated credential IDs are sufficient identifiers.

### Never accept tokens in argv

Do not add:

```text
--access-token
--refresh-token
```

Use `masked_secret_prompt()`.

### Never log credential objects

Avoid debug statements like:

```python
logger.debug("entry=%r", entry)
```

A dataclass repr can contain tokens.

If logging is necessary, log only:

```text
provider, credential id, label, action, result
```

### Keep mutations out of slash commands

Gateway users should not be able to paste OAuth secrets into chat or delete credentials through a conversational surface in v1.

### Do not export tokens

There is intentionally no `show-token`, `copy`, `export`, or `dump` command.

### Do not refresh during import

Refresh tokens may rotate/single-use. Let Hermes perform refresh when its runtime decides it is needed.

---

## 11. Compatibility and coupling

### What is stable

The plugin registration surface (`plugin.yaml`, `register(ctx)`, `register_cli_command`, `register_command`) is documented as the supported extension mechanism.

### What is coupled to Hermes internals

The credential manager necessarily imports implementation-level Hermes objects/functions:

- `PooledCredential`;
- `load_pool`;
- `SOURCE_MANUAL_DEVICE_CODE`;
- `write_credential_pool`;
- `mark_provider_active_if_unset`;
- `fetch_account_usage`.

These are present in current Hermes and the supplied 0.20.0 source, but they are not all documented as long-term third-party plugin APIs.

### Mitigation

Do not build an abstraction framework. Put all Hermes-specific imports near the top of `codex_pool.py` and fail with one clear message if an expected symbol is missing:

```text
hermes-codex-pool is incompatible with this Hermes version. Update the plugin.
```

Then test the plugin against Hermes `main` periodically.

The small code surface makes maintenance cheap.

### Version target

At research time, upstream `pyproject.toml` reports:

```text
hermes-agent 0.20.0
Python >=3.11,<3.14
```

The uploaded repository also reports `0.20.0`.

---

## 12. Multi-profile / concurrent-process behavior

Hermes now has profile/global-root auth-store semantics and cross-process locking. The plugin should not invent behavior around them.

Rules:

1. Call Hermes APIs in the **current Hermes environment/profile**.
2. Let `load_pool()` decide what entries are visible.
3. Let `pool.add_entry`, `pool.remove_index`, and `write_credential_pool` decide where/how writes happen.
4. Do not cache the pool globally across commands.
5. Load fresh state at the start of every command.
6. Do not maintain a daemon or watcher.

This naturally follows Hermes' current profile semantics without the plugin needing to understand every storage detail.

---

## 13. Status semantics

The dashboard needs to distinguish two kinds of information.

### Hermes local health state

Derived from `PooledCredential.last_status`:

- `dead` — Hermes considers the credential terminally invalid until replaced/re-added;
- `exhausted` — Hermes has persisted a cooldown/quota state;
- `available` — neither of those states is persisted.

### Provider usage state

Derived from `fetch_account_usage`:

- Session remaining/reset;
- Weekly remaining/reset;
- optional plan/reset-credit information.

Do not infer local pool health from a single diagnostics request. The two are displayed together but owned by different mechanisms.

Example:

```text
work  available
  Session  72% left · resets in 2h 14m
  Weekly   38% left · resets in 4d 8h
```

If usage fails:

```text
work  available
  usage unavailable
```

Do not silently rewrite `available` to `dead` because `/usage` returned an error.

---

## 14. Performance

V1 status can fetch accounts sequentially for simplicity.

Reason:

- status is an operator command, not the inference hot path;
- most users will have a small number of accounts;
- concurrency adds code and makes output/error handling more complex.

If real use shows status is too slow with many accounts, the first optimization should be a tiny `ThreadPoolExecutor(max_workers=min(4, len(entries)))` around usage fetches. Do not add caching or a background poller first.

`list` remains instant/local.

---

## 15. Error behavior

Keep errors actionable and short.

Examples:

```text
No Codex credentials found. Add one with: hermes codex-pool add
```

```text
No credential matching "foo".
```

```text
Label "work" already exists. Use a unique label.
```

```text
Usage unavailable for "work"; the credential was not modified.
```

```text
Hermes credential APIs expected by hermes-codex-pool are unavailable. Update the plugin or use a supported Hermes version.
```

Never include a raw exception if it may include Authorization headers/tokens. In verbose logs, sanitize aggressively or log only the exception class/status code.

---

## 16. Implementation order

### Phase 1 — skeleton

- [x] Create GitHub repository.
- [x] Add `plugin.yaml`.
- [x] Add tiny `__init__.py` registration.
- [x] Register `hermes codex-pool` with a default `status` command.
- [ ] Confirm install/enable from GitHub after pushing v0.1.0.

### Phase 2 — local credential CRUD

- [x] Implement `list` using `load_pool().entries()`.
- [x] Implement `add` using `masked_secret_prompt` + `PooledCredential` + `pool.add_entry`.
- [x] Match Hermes Codex fields exactly: OAuth + `manual:device_code`.
- [x] Call `mark_provider_active_if_unset` for first entry.
- [x] Implement target resolution with `pool.resolve_target`.
- [x] Implement `remove` with `pool.remove_index`.
- [x] Implement `rename` with `dataclasses.replace` + `write_credential_pool`.

### Phase 3 — usage dashboard

- [x] Implement `status [target]`.
- [x] Call `fetch_account_usage` with explicit entry token/base URL.
- [x] Render Session and Weekly remaining/reset.
- [x] Show Hermes strategy from `get_pool_strategy`.
- [x] Fail per-account usage lookup independently.

### Phase 4 — read-only slash command

- [x] Register `/codex-pool`.
- [x] Expose status/help only.
- [x] Explicitly reject mutation args.

### Phase 5 — tests/docs

- [x] Add isolated unit tests; no real OpenAI or Hermes-home access.
- [x] Test native add/list/rename/remove persistence against temporary `HERMES_HOME` on Hermes 0.20.4.
- [ ] Install from GitHub into a clean Hermes instance.
- [ ] Import two token pairs.
- [ ] Confirm `hermes auth list openai-codex` sees the same entries.
- [ ] Confirm `hermes codex-pool status` shows correct account usage.
- [ ] Run a real Hermes task and verify native Hermes rotation works without plugin intervention.
- [x] Document the stable v0.1.0 command output and compatibility boundary.

---

## 17. Acceptance criteria for v0.1.0

Release only when all of these are true:

1. Plugin installs from its own GitHub repository without modifying Hermes source.
2. `hermes codex-pool add` accepts access + refresh tokens through masked prompts.
3. Imported credentials appear in native `hermes auth list openai-codex` output.
4. Hermes can use the imported credential normally.
5. Hermes can refresh the imported OAuth entry using its existing runtime.
6. Multiple imported entries remain distinct.
7. Native Hermes failover still works when an entry is exhausted/rate-limited.
8. `hermes codex-pool list` performs no network request.
9. `hermes codex-pool status` reports Session and Weekly usage for each account when available.
10. Rename changes only the label.
11. Remove deletes only the selected native pool row.
12. No command prints or logs tokens.
13. `/codex-pool` is read-only.
14. The plugin creates no credential/state database of its own.
15. No request middleware, provider override, or custom rotation code exists in the plugin.

---

## 18. Known limitations — acceptable for v1

### Internal API coupling

The plugin registration mechanism is official, but the credential-pool mutation helpers are implementation-level Hermes APIs. A future Hermes refactor may require a small plugin compatibility update.

This is preferable to forking Hermes because the integration surface is tiny and isolated.

### Usage diagnostics may fail independently

An access token can be stale, the usage service can be unavailable, or provider behavior can change. `status` should degrade to `usage unavailable`; it must not corrupt the credential entry.

### No plugin-side refresh button

Intentional. Hermes should own refresh-token rotation.

### No exact `active account` marker across processes

A separate CLI invocation cannot safely claim which credential another running Hermes process is using at that instant. The plugin should show health/order/usage, not fabricate an active marker.

### Same-account duplicate token pairs

OAuth refresh tokens can rotate and may be single-use. Importing multiple independent token pairs for the same upstream account can create provider-side invalidation hazards. V1 should document that each pool entry is intended to represent a distinct account/token chain and should not attempt to solve cross-client refresh synchronization itself.

---

## 19. Future features — only if demanded

Do not implement these in v0.1.0. They are listed only so future work does not accidentally get mixed into the initial architecture.

Possible later additions:

- `enable/disable` if Hermes exposes a clean native concept for it;
- reorder priorities if users need fill-first ordering controls;
- `--json` output for scripting;
- parallel usage fetch if status latency becomes a demonstrated problem;
- a Desktop/Web dashboard view if Hermes exposes a stable credential-management extension API;
- exact-account duplicate detection if Hermes exposes a stable account identity field;
- upstream contribution to Hermes adding a public credential-management plugin API, which would remove the internal API coupling.

Not planned unless Hermes itself lacks required runtime behavior:

- custom switching policy;
- request middleware;
- quota wait/resume;
- token refresh engine.

---

## 20. Why this is simpler than porting pi-relay

A direct pi-relay port would duplicate systems Hermes already has:

```text
pi-relay vault                → Hermes auth.json credential_pool
pi-relay credential refresh   → Hermes OAuth refresh
pi-relay account selection    → Hermes credential pool strategy
pi-relay failure switching    → Hermes error recovery + rotation
pi-relay usage HTTP client    → Hermes account_usage
pi-relay file locking         → Hermes auth-store locking
```

The Hermes-specific plugin therefore only needs:

```text
paste/manage credentials + display account quota
```

That is the correct abstraction boundary.

---

## 21. Rough implementation size

Expected v1 production code:

- `__init__.py`: ~20 lines
- `codex_pool.py`: ~180–300 lines
- tests: ~250–400 lines
- no third-party runtime dependencies

If production code starts approaching ~500 lines, re-evaluate whether features have leaked in that Hermes already provides.

---

## 22. Reference implementation pseudocode

This is intentionally close enough to code that implementation can proceed without redesigning the architecture, while leaving exact formatting/testing decisions to the build step.

```python
# codex_pool.py
from __future__ import annotations

import uuid
from dataclasses import replace

from agent.account_usage import fetch_account_usage
from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    SOURCE_MANUAL_DEVICE_CODE,
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    PooledCredential,
    get_pool_strategy,
    load_pool,
)
from hermes_cli.auth import (
    DEFAULT_CODEX_BASE_URL,
    mark_provider_active_if_unset,
    write_credential_pool,
)
from hermes_cli.secret_prompt import masked_secret_prompt

PROVIDER = "openai-codex"


def setup_cli(parser):
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


def handle_cli(args):
    action = getattr(args, "codex_pool_action", None) or "status"
    if action == "add":
        print(add_account(getattr(args, "label", None)))
    elif action == "list":
        print(list_accounts())
    elif action == "status":
        print(status_accounts(getattr(args, "target", None)))
    elif action == "rename":
        print(rename_account(args.target, args.new_label))
    elif action == "remove":
        print(remove_account(args.target, yes=args.yes))


def add_account(label=None):
    pool = load_pool(PROVIDER)
    entries = pool.entries()
    label = (label or "").strip() or f"openai-codex-oauth-{len(entries) + 1}"
    # duplicate-label check

    access = masked_secret_prompt("Access token: ").strip()
    refresh = masked_secret_prompt("Refresh token: ").strip()
    if not access or not refresh:
        return "Access and refresh tokens are required."

    if any(e.access_token == access and e.refresh_token == refresh for e in entries):
        return "That token pair is already present in the Codex pool."

    first = not entries
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
    )
    added = pool.add_entry(entry)
    if first:
        mark_provider_active_if_unset(PROVIDER)
    return f'Added Codex credential "{added.label}" ({added.id}).'


def list_accounts():
    pool = load_pool(PROVIDER)
    entries = pool.entries()
    if not entries:
        return "No Codex credentials found. Add one with: hermes codex-pool add"
    # format rows; no network


def status_accounts(target=None):
    pool = load_pool(PROVIDER)
    entries = pool.entries()
    if target:
        index, entry, error = pool.resolve_target(target)
        if error:
            return error
        entries = [entry]

    # for each entry:
    # snapshot = fetch_account_usage(
    #     PROVIDER,
    #     base_url=entry.runtime_base_url or DEFAULT_CODEX_BASE_URL,
    #     api_key=entry.runtime_api_key,
    # )
    # render local state + Session/Weekly data


def rename_account(target, new_label):
    new_label = new_label.strip()
    if not new_label:
        return "Label cannot be empty."
    pool = load_pool(PROVIDER)
    index, entry, error = pool.resolve_target(target)
    if error:
        return error
    entries = pool.entries()
    # duplicate-label check excluding entry.id
    updated = [replace(e, label=new_label) if e.id == entry.id else e for e in entries]
    write_credential_pool(PROVIDER, [e.to_dict() for e in updated])
    return f'Renamed "{entry.label}" to "{new_label}".'


def remove_account(target, *, yes=False):
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


def handle_slash(raw_args):
    action = raw_args.strip().lower()
    if action in {"", "status"}:
        return status_accounts()
    if action == "help":
        return "Usage: /codex-pool [status|help]"
    return "Credential changes are terminal-only. Use: hermes codex-pool --help"
```

During implementation, compare this pseudocode against the exact installed Hermes version before copying imports verbatim.

---

## 23. Final architectural rule

When deciding whether a feature belongs in this plugin, use this test:

> **Does this feature manage/inspect the credential records, or does it decide how an inference request uses them?**

- **Manage/inspect records** → plugin.
- **Decide how inference uses them** → Hermes.

Keeping that boundary is what makes this extension small, reliable, and maintainable.

---

## 24. Research sources

### Official Hermes documentation

1. **Build a Hermes Plugin**  
   https://hermes-agent.nousresearch.com/docs/developer-guide/plugins

2. **Credential Pools**  
   https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools

### Current Hermes upstream source researched on 2026-08-11

3. **PluginContext / plugin loader**  
   https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/plugins.py

4. **Credential pool**  
   https://github.com/NousResearch/hermes-agent/blob/main/agent/credential_pool.py

5. **Auth/persistence helpers**  
   https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/auth.py

6. **Built-in auth commands / Codex add flow**  
   https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/auth_commands.py

7. **Account usage / Codex quota parser**  
   https://github.com/NousResearch/hermes-agent/blob/main/agent/account_usage.py

8. **Hermes package metadata**  
   https://github.com/NousResearch/hermes-agent/blob/main/pyproject.toml

### Local repositories reviewed

9. Uploaded `hermes-agent-main` (`0.20.0`) — used to verify the same core plugin/credential APIs in the supplied source snapshot.

10. Uploaded `pi-relay-main` — used to identify the useful management/status UX to retain while deliberately excluding its routing/vault/refresh/wait layers from the Hermes design.

---

**Recommended v0.1.0 scope:** implement exactly the command surface in Section 5 and stop. Anything beyond that should require a concrete user problem rather than parity with pi-relay.
