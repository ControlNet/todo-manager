# Todo Manager

**Your email, Slack and GitHub, turned into a todo list you never had to write, in Notion, with
an AI agent that can take some of the work off your plate.**

Todo Manager is an add-on for [Hermes](https://github.com/NousResearch/hermes-agent), the
self-hosted AI agent. It reads your conversations, keeps the things that are really yours as
tasks, and shows them in a Notion page you already know how to use.

## Why

You make promises all day, in many places: "sure, I'll look at it before Friday" in an email, a
question in a Slack DM, a review request on a pull request. A todo app only helps if you write
each one down; inbox zero only helps if you triage everything yourself.

Todo Manager reads the conversations for you. Only what is actually yours ends up on the list,
with the deadline, the background and a link back to where it came from. Everything else is
set aside with a one-line reason, so nothing is lost and you never face a "please triage" pile.

## What it is like

- **A colleague emails:** "Could you review the draft before Friday?" Once the thread has gone
  quiet, a task appears in Notion: *Review the draft*, due Friday, with why it is yours and a
  link to the mail.
- **Newsletters, notifications, threads you were only copied on** are dismissed, each with a
  short reason you can look up later.
- **Something only you can decide** lands on the *To decide* board. Drag the card to *Done*,
  *Cancel*, *My to-do* or *Assign to Agent*.
- **Assign to Agent** hands the task to a Hermes agent. If it needs something, it asks in a
  comment on the task and you answer there. The result arrives as a delivery page under the
  task, with its files, PDFs and images.
- **Talk to it in plain words.** Comment "the deadline moved to Monday", or tell it "from now
  on, ignore mail from this list". Lasting conventions go into a `PREFERENCES.md` you can read
  and edit.
- **It notices when you are done.** When you answer the mail or your pull request is merged,
  the task is closed and the evidence is attached to it.

## Your Notion home page

```
Todo Manager
  Data freshness: all sources are syncing normally
  🤔 To decide            board: Pending · Cancel · Done · My to-do · Assign to Agent
  🤖 Agent work           board: Needs my answer · To review · In progress · ...
  ⭐ Focus                what to do now: due within a week, or marked as focus
  📁 Active projects
  ⏳ Waiting on others    sorted by when to follow up
  📋 All open tasks
  🕘 Recently done and notable changes
```

Every task page has one box that Todo Manager keeps up to date (why the task is yours, what
"done" means, the sources). Everything outside that box is yours: your notes stay exactly as
you wrote them, and the agent reads them as context.

## You stay in charge

- **Your edits win.** Change a field in Notion and no later AI judgement overwrites it. A task
  you closed is never reopened by itself.
- **Nothing disappears silently.** Every new message is either linked to a task or dismissed
  with a reason.
- **Agents ask before acting outside.** An agent may send an email or take a similar external
  action only when you allowed it for that task or approve it in a comment.
- **Todo Manager only reads** your mail, Slack and GitHub. It never posts there itself. The one
  exception is opt-in: Gmail labels that show which project a mail belongs to and whether its
  task is still open.
- **Quiet when nothing happens.** Every five minutes plain code checks for news; the AI model is
  woken only when there is something new to judge.
- **English or Simplified Chinese**, for everything you read.

## What you need

- A Linux machine running Hermes, with its cron and Kanban features
- A Notion workspace
- Any of: Gmail, Slack, GitHub (each is optional)

<details>
<summary>Exact requirements</summary>

- **Linux.** File locks use `fcntl`; the optional real-time sync runs as a `systemd --user`
  service. Other systems are untested.
- **Python ≥ 3.11**, standard library only; nothing to `pip install`.
- **Hermes** with cron and Kanban, verified against Hermes 0.21.5 (commit `105568c`). The board
  bridge uses Hermes' Python modules, so other versions may need changes; see
  [docs/hermes-native-verification.md](docs/hermes-native-verification.md).
- **Notion:** an internal integration whose token is in `NOTION_API_KEY`, with the read content,
  update content, insert content, read comments and insert comments capabilities.
- **Sources** (`install` finds what Hermes already has):
  - Gmail: a himalaya account in Hermes whose `gmail.auth.token.command` prints an OAuth access
    token (read-only Gmail access is enough; the optional Gmail labels need `gmail.modify`);
  - Slack: a user token (`SLACK_USER_TOKEN`, or `$HERMES_HOME/credentials/slack_user_token`)
    that can call `users.conversations`, `conversations.history`, `conversations.replies` and
    `users.info` for the conversation types you include;
  - GitHub: the `gh` CLI, logged in (`gh auth login`).

</details>

## Get started

1. **In Notion**, create an empty top-level page. It becomes your Todo Manager home. Add your
   integration to it (`···` → *Connections*).
2. **On the machine running Hermes:**

   ```bash
   git clone https://github.com/ControlNet/todo-manager.git
   cd todo-manager
   python3 -B -m tests.run --all      # optional self-check, under a minute; expect "OK"
   ./bin/todo-manager install
   ```

   `install` works out most settings from Hermes (language, time zone, mailboxes, Slack,
   GitHub, Notion) and asks only real choices: which page is home, which mailboxes and
   conversations to read, and whether to start right away. It shows every change before making
   it, and is safe to run again.
3. **Open your home page in Notion.** Tasks appear there as recent conversations are read and
   judged.

Keep the cloned folder where it is: the scheduled jobs run from it, so changes to it take effect
at once. To hack on the code, use a separate clone or a `git worktree`.

## Day to day

You mostly live in Notion. On the command line:

```bash
todo-manager status                                       # is everything healthy?
todo-manager pause                                        # stop the scheduled jobs for a while
todo-manager resume
todo-manager config set slack.quiet_minutes 60 --human    # e.g. wait an hour of Slack silence
```

**Optional: Gmail labels.** Mail linked to a task can carry `Todo/<project>` and
`Todo/<project>/<Open|Done|Cancelled>` in Gmail. `todo-manager gmail-labels show` lists the
labels and how many mails each would get; `todo-manager config set email.labels true --human`
turns it on. See [docs/operations.md](docs/operations.md#gmail-labels).

You can also ask Hermes in chat to change a setting or a convention. Problems (a source that
stopped syncing, an agent that got stuck) show up in the *Data freshness* box on the home page
and in `todo-manager status`. More in [docs/operations.md](docs/operations.md).

**Optional: real-time sync.** By default Notion is checked every five minutes. A small background
service picks up your comments and edits within seconds instead; it needs a public HTTPS address
for Notion's webhooks, for example through a Cloudflare Tunnel. See
[docs/realtime-sync.md](docs/realtime-sync.md).

## Your data

Everything Todo Manager reads is stored in a local SQLite database in its workspace
(`~/.hermes/workspace/todo-manager` by default), never in this folder. Material that needs
judging is shown to the AI model your Hermes is configured with, and your tasks are written to
your Notion. Your Notion, Slack and GitHub tokens stay where Hermes keeps them; the settings
file only names them.
`todo-manager export` writes a readable copy of everything; `todo-manager uninstall` removes the
scheduled jobs and keeps your data and Notion pages.

## Status

A personal project, in daily use by its author. It is built for one person on one machine.
How well the AI judges what is yours has not been formally measured yet; see
[docs/limitations.md](docs/limitations.md).

## Documentation

- **Using it:** [operations](docs/operations.md) (install, upgrade, health, pause, export,
  backup, uninstall) · [Notion pages and fields](docs/notion-schema.md) ·
  [real-time sync](docs/realtime-sync.md)
- **How it works:** [architecture](docs/architecture.md) (what Hermes does, what Todo Manager
  does) · [agent work on the Kanban board](docs/board-protocol.md)
- **Reference:** [command line](docs/cli.md) · [limitations](docs/limitations.md) ·
  [acceptance criteria](docs/acceptance.md) · [open decisions](docs/open-decisions.md) ·
  [Hermes verification](docs/hermes-native-verification.md)

An example of conventions for academic work is in
[`todo_manager/hermes_assets/examples/academic.md`](todo_manager/hermes_assets/examples/academic.md).

## License

[AGPL-3.0-only](LICENSE).
