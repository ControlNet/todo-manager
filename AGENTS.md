# Environment

- Todo Manager runtime and unit tests use Python standard library only.
- For real Hermes integration tests, the user explicitly authorized the existing
  `~/.hermes/hermes-agent/venv` on 2026-10-06. Do not install or change its dependencies.
- Hermes source: `~/.hermes/hermes-agent`.
- Integration tests must use temporary HERMES_HOME, boards and workspaces; no real model,
  production Notion, channel delivery, production cron or global configuration changes.
