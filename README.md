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
  to ask: `PREFERENCES.md` in the workspace, written by you or by Hermes when you ask in chat
  or in a Notion comment, and given to every run as written. Settings such as
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
                                                                                             │ todo-manager task/source/… commands
 Notion (home, Tasks, Projects) ◀──── push / import ────▶  SQLite (source of truth)  ◀───────┘
        comments, edits ──▶ feedback gate ──(needs judgement)──▶ Hermes ──▶ commands
                                     │
                                     └──▶ Hermes Kanban board "todo-manager" (delegations, scheduled by Hermes)
```

## Requirements

- **Linux.** File locks use `fcntl`; the optional real-time daemon is a `systemd --user`
  service. Other systems are untested.
- **Python ≥ 3.11**, standard library only.
- **Hermes** with cron and Kanban, verified against Hermes 0.21.5 (commit `105568c`). The board
  bridge uses Hermes' Python modules, so other versions may need changes; see
  [docs/hermes-native-verification.md](docs/hermes-native-verification.md).
- **Notion:** an internal integration whose token is in `NOTION_API_KEY`, with the read content,
  update content, insert content, read comments and insert comments capabilities.
- **Sources** (each optional; `install` finds what Hermes already has):
  - Gmail: a himalaya account in Hermes whose `gmail.auth.token.command` prints an OAuth access
    token (read-only Gmail access is enough);
  - Slack: a user token (`SLACK_USER_TOKEN`, or `$HERMES_HOME/credentials/slack_user_token`)
    that can call `users.conversations`, `conversations.history`, `conversations.replies` and
    `users.info` for the conversation types you include;
  - GitHub: the `gh` CLI, logged in (`gh auth login`).

Todo Manager only reads your sources. What it reads is stored in the local workspace and shown
to the model your Hermes is configured with; your tasks are written to your Notion.

## Quick start

First create an empty top-level Notion page (the future home page) and add your Notion
integration to it (··· → Connections). Then:

```bash
git clone https://github.com/ControlNet/todo-manager.git
cd todo-manager
python3 -B -m tests.run --all                   # parallel, under a minute; expect OK (Hermes tests skip without Hermes)
./bin/todo-manager install
```

`install` points the Hermes jobs (and the optional daemon) at this checkout: keep it where it is,
and note that edits in it take effect at once. Develop in a separate clone or `git worktree`.

`install` infers the rest from Hermes (language, time zone, mailboxes, Slack, GitHub, Notion
and Kanban settings), asks real choices (the home page, source scopes, execution profile and whether new jobs start) and shows every change outside the workspace before making it. It is safe to re-run;
see [docs/operations.md](docs/operations.md#install).

For this breaking CLI release, drain old callers and install from a separate checkout using `./bin/todo-manager install` ([transition procedure](docs/cli.md#switching-from-the-old-cli)) (managed skill, prompts and AGENTS are
backed up before replacement; all existing cron runtime settings are preserved); remove with `todo-manager uninstall`
(all data stays). See [docs/operations.md](docs/operations.md#upgrade).

Runtime data (database, runs, exports, backups) lives only in the workspace
(`TODO_HOME`, default `~/.hermes/workspace/todo-manager`); the tool refuses a workspace inside
the source tree. Tokens are read from environment variables (for
example `NOTION_API_KEY`); never commit `.env` / `.env.local` files (`.gitignore` covers them).

## Documentation

- [docs/cli.md](docs/cli.md) – command contracts, explicit provenance, settlement and release transition
- [docs/hermes-native-verification.md](docs/hermes-native-verification.md) – source evidence, native tests, migration and boundaries
- [docs/architecture.md](docs/architecture.md) – what Hermes does and what Todo Manager does,
  the two jobs, authority, ownership of files and settings
- [docs/operations.md](docs/operations.md) – install, upgrade, changing the automation, health,
  pause/resume, uninstall, export, backup/restore, migration
- [docs/notion-schema.md](docs/notion-schema.md) – Notion structure, field mapping, home views,
  sync rules
- [docs/board-protocol.md](docs/board-protocol.md) – how delegations use the Hermes Kanban board
- [docs/acceptance.md](docs/acceptance.md) – acceptance criteria and how each is verified
- [docs/realtime-sync.md](docs/realtime-sync.md) – optional real-time sync daemon: Notion
  webhooks, local and board changes synced at once (off by default)
- [docs/open-decisions.md](docs/open-decisions.md) – proposed defaults and user decisions
- [docs/limitations.md](docs/limitations.md) – known limitations

The Hermes skill, the cron prompts and the workspace `AGENTS.md` template ship in
`todo_manager/hermes_assets/` (an optional example of conventions for academics:
`hermes_assets/examples/academic.md`).

## License

[AGPL-3.0-only](LICENSE).
