# Todo Manager

A personal todo system for [Hermes](https://github.com/NousResearch/hermes-agent) users. It
reads your email, Slack and GitHub, keeps the commitments that are really yours as tasks in a
local database, shows them in Notion where you read and edit, and hands explicit work to agents
through a Hermes Kanban board.

It is a Todo automation **for** Hermes: Hermes runs the agent, its judgement, your conventions
and skills, the board's workers, memory and delivery; Todo Manager adds what Hermes does not
have - incremental reading of the sources, the task data and its history, the Notion UI and its
sync, the durable link between a task and its agent cards, and install/upgrade/export. See
[docs/architecture.md](docs/architecture.md).

- **Evidence, not keywords.** Hermes judges new conversations with their full context once a
  thread has gone quiet. Every new item ends up either linked to a task or dismissed with a
  reason; nothing is dropped silently, and there is no "please triage" inbox.
- **Your conventions, in your words.** What counts as your responsibility, what to ignore, when
  to ask: native `<profile home>/memories/USER.md`, maintained by Hermes on your explicit request. Settings such as
  "wait an hour of Slack silence" are `config.toml` values (`todo-manager config set`).
- **Your edits win.** Changes you make in Notion properties are imported as yours; agent
  judgements based on older versions never overwrite them, and tasks you closed are never
  reopened automatically. Comments are discussion and instructions; your notes on a page are
  context.
- **Cheap when idle.** Two Hermes cron jobs run deterministic pre-run gates every five minutes
  and wake the model only when there is something to judge; the gate hands it the material
  directly.
- **Agent work on the Hermes board.** A Notion task can be delegated through an entry card on the board
  `todo-manager`; Hermes schedules and runs it. Questions and late answers stay on the card that
  asked, results come back to the task, closing the task cancels the work, and external actions
  happen only when allowed or approved.
- **Localized.** Everything you read follows `language` (English or Simplified Chinese);
  prompts for the model stay in English.

```
 Gmail / Slack / GitHub ──▶ intake gate ──(mature conversations, in the context)──▶ Hermes ──┐
                                                                                             │ todo-manager task/item/… commands
 Notion (home, Tasks, Projects) ◀──── push / import ────▶  SQLite (source of truth)  ◀───────┘
        comments, edits ──▶ feedback gate ──(needs judgement)──▶ Hermes ──▶ commands
                                     │
                                     └──▶ Hermes Kanban board "todo-manager" (delegations, scheduled by Hermes)
```

## Quick start

Requires Python ≥ 3.11 (standard library only) and Hermes with cron and Kanban.

First create an empty top-level Notion page (the future home page) and add your Notion
integration to it (··· → Connections). Then:

```bash
git clone https://github.com/ControlNet/todo-manager.git ~/src/todo-manager && cd ~/src/todo-manager
python3 -m unittest discover -s tests -t .      # expect OK
./bin/todo-manager install
```

`install` infers the rest from Hermes (language, time zone, mailboxes, Slack, GitHub, Notion
and Kanban settings), asks only real choices (the home page, which mailboxes when there are
several) and shows every change outside the workspace before making it. It is safe to re-run;
see [docs/operations.md](docs/operations.md#install).

Upgrade with `git pull` and `./bin/todo-manager install` (managed skill, prompts and AGENTS are
backed up before replacement; all existing cron runtime settings are preserved); remove with `todo-manager uninstall`
(all data stays). See [docs/operations.md](docs/operations.md#upgrade).

Runtime data (database, runs, exports, backups) lives only in the workspace
(`TODO_HOME`, default `~/.hermes/workspace/todo-manager`); the tool refuses a workspace inside
the source tree. Tokens are read from environment variables (for example `NOTION_API_KEY`);
never commit `.env` / `.env.local` files.

## Documentation

- [docs/hermes-native-verification.md](docs/hermes-native-verification.md) – source evidence, native tests, migration and boundaries
- [docs/architecture.md](docs/architecture.md) – what Hermes does and what Todo Manager does,
  the two jobs, authority, ownership of files and settings
- [docs/operations.md](docs/operations.md) – install, upgrade, changing the automation, health,
  pause/resume, uninstall, export, backup/restore, migration
- [docs/notion-schema.md](docs/notion-schema.md) – Notion structure, field mapping, home views,
  sync rules
- [docs/board-protocol.md](docs/board-protocol.md) – how delegations use the Hermes Kanban board
- [docs/acceptance.md](docs/acceptance.md) – acceptance criteria and how each is verified
- [docs/open-decisions.md](docs/open-decisions.md) – proposed defaults and user decisions
- [docs/limitations.md](docs/limitations.md) – known limitations

The Hermes skill, the cron prompts and the workspace `AGENTS.md` template ship in
`todo_manager/hermes_assets/` (an optional example of conventions for academics:
`hermes_assets/examples/academic.md`).

## License

[AGPL-3.0-only](LICENSE).
