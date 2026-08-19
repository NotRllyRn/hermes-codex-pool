# Changelog

All notable changes are documented here. Entries that require operator action are prefixed **BREAKING**.

## [Unreleased]

_No changes._

## [0.1.0] - 2026-08-19

- Add terminal-only Codex OAuth credential import, list, rename, and remove commands.
- Add per-account Session/Weekly usage status using Hermes' native usage client.
- Add read-only `/codex-pool` status and help command.
- Store all credentials through Hermes' native credential pool; no plugin state or router.
- Target Hermes Agent 0.20.0 and source-check compatibility with 0.20.4.
