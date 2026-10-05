# Todo Manager

A personal todo system for [Hermes](https://github.com/NousResearch/hermes-agent) users. It
reads your email, Slack and GitHub, keeps the commitments that are really yours as tasks in a
local database, shows them in Notion where you read and edit, and hands explicit work to agents
through a Hermes Kanban board.

- **Evidence, not keywords.** A model judges new conversations with their full context once a
  thread has gone quiet. Every new item ends up either linked to a task or ignored with a
  category and a reason; nothing is dropped silently, and there is no "please triage" inbox.
- **Your edits win.** Changes you make in Notion (properties, comments, the page body) are
  imported as yours; agent judgements based on older versions never overwrite them, and tasks
  you closed are never reopened automatically.
- **Cheap when idle.** Two Hermes cron jobs run deterministic pre-run gates every five minutes
  and wake the model only when there is something to judge.
- **Safe delegation.** One card per delegation, duplicate-proof submission and recovery,
  questions that block until you answer, deliveries that wait for your review, and external
  actions only when allowed or approved for the exact delivered version.
- **Localized.** Everything you read follows `language` (English or Simplified Chinese);
  prompts for the model stay in English.

```
 Gmail / Slack / GitHub ──▶ intake gate ──(mature batches)──▶ model ──▶ decisions ──┐
                                                                                    ▼
 Notion (home, Tasks, Projects) ◀──── push / import ────▶  SQLite (source of truth)  ◀── todo-manager CLI
        comments, edits ──▶ feedback gate ──(needs judgement)──▶ model ──▶ decisions ──┘
                                     │
                                     └──▶ Hermes Kanban board "todo-manager" (delegations)
```

## Quick start

Requires Python ≥ 3.11 (standard library only) and Hermes with cron and Kanban.

First create an empty top-level Notion page (the future home page) and add your Notion
integration to it (··· → Connections). Then:

```bash
git clone <repository url> ~/src/todo-manager && cd ~/src/todo-manager
python3 -m unittest discover -s tests -t .      # expect OK
./bin/todo-manager install
```

`install` infers the rest from Hermes (language, time zone, mailboxes, Slack, GitHub, Notion
and Kanban settings), asks only real choices (the home page, which mailboxes when there are
several) and shows every change outside the workspace before making it. It is safe to re-run;
see [docs/operations.md](docs/operations.md#install).

Runtime data (database, runs, exports, backups) lives only in the workspace
(`TODO_HOME`, default `~/.hermes/workspace/todo-manager`); the tool refuses a workspace inside
the source tree. Tokens are read from environment variables (for example `NOTION_API_KEY`);
never commit `.env` / `.env.local` files.

## Documentation

- [docs/operations.md](docs/operations.md) – install, health, metrics, pause/resume/rollback,
  export, backup/restore, migration
- [docs/notion-schema.md](docs/notion-schema.md) – Notion structure, field mapping, home views,
  sync rules
- [docs/board-protocol.md](docs/board-protocol.md) – Hermes Kanban protocol and required settings
- [docs/acceptance.md](docs/acceptance.md) – acceptance criteria and how each is verified
- [docs/open-decisions.md](docs/open-decisions.md) – proposed defaults and user decisions
- [docs/limitations.md](docs/limitations.md) – known limitations

The Hermes skill and cron prompts ship in `todo_manager/hermes_assets/`; the authoritative
decision formats the model follows are in `todo_manager/prompts/`.

## License

[AGPL-3.0-only](LICENSE).
