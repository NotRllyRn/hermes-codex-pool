# hermes-codex-pool

Manage multiple OpenAI Codex OAuth accounts in Hermes Agent, prefer the healthy account whose Weekly quota resets first, and inspect per-account usage.

The plugin supplies policy and UI only. Hermes validates each preference and continues to own OAuth refresh, cooldowns, retries, credential switching, and automatic failover.

## Compatibility

- Plugin: **0.2.0**
- Credential management baseline: **Hermes Agent 0.20.0**
- Automatic preference requires the post-0.20.4 `pre_credential_select` and credential-identity hook contract
- Python: **3.11–3.13**

On older Hermes versions, use v0.1.0. Review [COMPATIBILITY.md](COMPATIBILITY.md) before upgrading.

## Install

```bash
hermes plugins install NotRllyRn/hermes-codex-pool --no-enable
hermes plugins enable hermes-codex-pool
```

Then verify discovery:

```bash
hermes plugins list
```

## Usage

Show all accounts and quota:

```bash
hermes codex-pool
hermes codex-pool status
```

Import a token pair through masked prompts:

```bash
hermes codex-pool add --label work
```

Tokens are never accepted as command-line flags. They are written directly to Hermes' native `openai-codex` credential pool.

Other commands:

```bash
hermes codex-pool list
hermes codex-pool status work       # ID, unique label, or 1-based index
hermes codex-pool rename work work-main
hermes codex-pool remove work
hermes codex-pool remove work --yes
```

Inside a Hermes session:

```text
/codex-pool
/codex-pool status
/codex-pool help
```

The slash command is read-only because it is also available through gateways such as Telegram and Discord. Credential mutations remain terminal-only.

## Storage and security

There is no plugin-specific database. Hermes stores credentials in its normal auth store (typically `~/.hermes/auth.json`) through its locking and credential-pool APIs.

- Do not share `auth.json` or include it in bug reports.
- Use a distinct account/token chain for each row.
- Do not continuously share a rotating refresh token with another client; Hermes becomes its runtime owner after import.
- A diagnostics failure only displays `usage unavailable`; it does not alter credential health.

## Automatic preference

Once per Codex turn, the plugin:

1. considers locally healthy native pool entries;
2. reads each entry's Weekly usage window through Hermes;
3. requests the entry with the earliest valid Weekly reset;
4. lets Hermes accept or reject that opaque credential ID.

The first provider request then emits one truthful line using the identity Hermes actually reports:

```text
[Codex: work · Weekly 38% left · resets in 18h · earliest weekly reset]
```

If usage is incomplete, the plugin returns no preference and stock Hermes routing continues. Hermes may still rotate after refresh, authentication, quota, or rate-limit failures.

## What Hermes handles

Hermes handles credential validation, same-provider switching, rotation strategy, OAuth refresh, cooldowns, retries, rate/quota-limit failover, and cross-provider fallback. This plugin does not intercept model requests or replace those systems.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests use fakes: they make no OpenAI requests and do not access the user's Hermes home.

## License

MIT
