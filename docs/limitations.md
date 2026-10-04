# Known limitations

Concrete, current limitations of this version. See [acceptance.md](acceptance.md) for what is
verified and how.

## Verification

- **Model judgement is not evaluated yet.** Whether intake creates the right tasks (and ignores
  the right items), and whether feedback is classified correctly, has not been measured on real
  data. The code enforces structure (full coverage of new items, categories and reasons for
  ignores, versioned writes), not accuracy.
- **Slack has not run against a real workspace.** The adapter is covered by recorded-shape tests
  (`tests/test_sources.py`, fake HTTP) only. Gmail and GitHub adapters are covered by the same kind
  of tests and have been run read-only against real accounts (fetch, batching and context
  rendering; no model decisions).
- **Notion writes are tested against a test double.** `tests/fake_notion.py` imitates the
  2026-03-11 API (data sources, property ids, markdown and views endpoints, comments, minute-
  precision `last_edited_time`, lost responses, 429). Real-API differences can surface on the
  first production setup; it is idempotent and can be re-run.
- **Kanban** is tested both with `FakeKanban` and against the installed Hermes Kanban code in an
  isolated `HERMES_HOME`; the worker is simulated (`tests/integration/hermes_worker_sim.py`),
  not a real model.
- **Matrix reminders** (`hermes send`) are tested with a fake runner only.

## Behaviour

- Notion is polled, not pushed: edits are imported within ~5 minutes; comments on pages
  without recent activity are polled round-robin (default hourly), so a comment there can take
  up to that long to be noticed.
- The Notion structure language is fixed at setup. Changing `language` later changes replies,
  reminders and new text, but property and option names stay as created (sync keeps working
  because it reads by property id and accepts option names of every language).
- Supported languages: English and Simplified Chinese. Adding one means adding a catalog in
  `todo_manager/locales/` and registering it in `i18n.LANGUAGES`.
- Unthreaded Slack chat is batched per channel/DM, not per topic.
- Matrix reminders are at-most-once: a send that timed out is not retried, because the outcome
  cannot be looked up.
- One user, one machine, one SQLite database. There is no multi-user or multi-host mode.
- The Hermes Kanban switches `review_dispatch` and `auto_decompose` are global in Hermes; a
  human-review board needs both off for all boards.
- The migration importer knows one legacy schema (`migration.LEGACY`); other legacy databases
  need that mapping adapted.
- Not supported by design: Zoom transcripts, Notion webhooks, paid Notion features, a model
  call per commit, reading chat logs as cron input.

## Security

- Source content is treated as evidence and the prompts say so, and every Hermes prompt
  surface passes the Hermes prompt scanner in tests, but prompt-injection resistance ultimately
  relies on the model. External actions are additionally gated by per-delegation allowed
  actions and version-bound approvals.
- Tokens are read from environment variables; the workspace must not be committed or shared.
