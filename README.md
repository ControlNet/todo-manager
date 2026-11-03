# Todo Manager

**Your AI agent reads your email, Slack and GitHub and keeps your todo list in Notion.
When the agent can do a task itself, you hand it over with one drag.**

Todo Manager is an add-on for [Hermes](https://github.com/NousResearch/hermes-agent), the
self-hosted AI agent, and uses the AI model and agent board you already have there.

![How Todo Manager works: your Gmail, Slack and GitHub inbox is checked every 5 minutes; new messages go to the Hermes LLM judge, which keeps your tasks in a local store; Notion and the Web UI show them and send your edits and comments back; a card you drag to Agent work goes to a Hermes agent, whose result comes back to the task.](docs/images/how-it-works.svg)

## What it does

- **Turns messages into tasks.** "Could you review this before Friday?" becomes a task due
  Friday, linked to the email. Newsletters and threads you were only copied on are skipped.
- **Puts your decisions on one board.** Drag a card to *Done*, *Cancel*, *My to-do* or
  *Assign to Agent*.
- **Hands work to an agent.** It drafts, researches or writes, asks in a comment when it needs
  something, and puts the result on a page under the task.
- **Understands plain words.** Comment "the deadline moved to Monday" on a task, or tell it
  "from now on, ignore mail from this list".
- **Closes tasks for you** when you reply to the email or your pull request is merged.

Prefer a browser? The optional [Web UI](docs/web-ui.md) shows the same tasks on desktop, tablet
and phone.

## Get started

You need a Linux machine with [Hermes](https://github.com/NousResearch/hermes-agent) (tested
with 0.21 and 0.22), Python 3.11 or newer, a free Notion account, and at least one of:

- **Gmail**: the account your Hermes already uses (through himalaya)
- **Slack**: a user token in `~/.hermes/.env` as `SLACK_USER_TOKEN`
- **GitHub**: the `gh` command, logged in with `gh auth login`

Handing work to an agent also needs Hermes' Kanban board.

1. **Connect Notion.** At [notion.so/my-integrations](https://www.notion.so/my-integrations),
   create an internal integration that can read, update and insert content and read and insert
   comments. Save its secret:

   ```bash
   echo 'NOTION_API_KEY=paste-your-secret-here' >> ~/.hermes/.env
   ```

   Then create an empty Notion page and add the integration under `···` → *Connections*.

2. **Install.**

   ```bash
   git clone https://github.com/ControlNet/todo-manager.git
   cd todo-manager
   ./bin/todo-manager install
   ```

   The installer reads your Hermes setup, asks which Notion page, mailboxes and chats to use, and
   shows every change before making it. Keep the folder: Todo Manager runs from it.

3. **Open your Notion page.** Tasks from your recent conversations appear within a few minutes.

## Everyday use

You work in Notion: drag cards, comment, add notes. If something stops working, it shows at the
top of the page. The `todo-manager` command covers the rest:

```bash
todo-manager status     # is everything working?
todo-manager pause      # stop for a while
todo-manager resume     # start again
todo-manager export     # save a readable copy of all your tasks
todo-manager uninstall  # stop for good; your data and Notion pages are kept
```

To change a setting, ask Hermes, for example "read Slack only after an hour of silence".

## Good to know

- **Your data stays on your machine.** Tasks go to your Notion, and new messages go to the AI
  model your Hermes uses. The model runs only when there is something new.
- **Your edits win.** A field you change in Notion keeps your value.
- **Agents ask before sending email.** An agent sends one only after you approve it in a
  comment. This is an instruction the agent follows, not a technical block.
- **The AI makes mistakes.** Every message it skips is kept with a reason; tell it what it got
  wrong and it adjusts.
- Built for one person on one machine, in English or Simplified Chinese.

## Learn more

- [Operations guide](docs/operations.md): upgrade, backup, instant sync, Gmail labels,
  troubleshooting
- [Notion pages and fields](docs/notion-schema.md) · [Command line](docs/cli.md) ·
  [How it works](docs/architecture.md) · [Known limitations](docs/limitations.md)

## License

[AGPL-3.0](LICENSE)
