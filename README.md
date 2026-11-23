# Todo Manager

**Your AI agent reads your email, Slack and GitHub and keeps your todo list in Notion.
When the agent can do a task itself, you hand it over with one drag.**

Todo Manager is an add-on for [Hermes](https://github.com/NousResearch/hermes-agent), the
self-hosted AI agent, and uses the AI model and agent board you already have there.

![How Todo Manager works: your Gmail, Slack and GitHub inbox is checked every 5 minutes; new messages go to the Hermes LLM judge, which keeps your tasks in a local store; Notion and the Web UI show them and send your edits and comments back, and other AI agents reach them through the command line, the HTTP API or MCP; a card you drag to Assign to Agent goes to a Hermes agent, whose result comes back to the task.](docs/images/how-it-works.svg)

## What it does

- **Someone asks you for something, and it shows up as a task.** "Could you review this before
  Friday?" becomes a task due Friday, linked to the email. Newsletters and threads you were
  only copied on are left out.
- **You decide with one drag.** Move a card to *Done*, *Cancel*, *My to-do* or
  *Assign to Agent*.
- **An agent can do the work.** Give it a card and it drafts, researches or writes, asks in a
  comment when it needs something, and puts the result on a page under the task.
- **You talk to it in plain words.** Comment "the deadline moved to Monday" on a task, or tell
  it "from now on, ignore mail from this list".
- **Finished work closes itself.** Reply to the email or get your pull request merged, and the
  task is closed for you.

Prefer a browser? The optional [Web UI](docs/web-ui.md) shows the same tasks on desktop, tablet
and phone.

## Get started

You need a Linux machine with [Hermes](https://github.com/NousResearch/hermes-agent) (tested
with 0.21 and 0.22), Python 3.11 or newer, a free Notion account, and at least one of:

- **Gmail**: the account your Hermes already uses (through himalaya)
- **Slack**: a user token in `~/.hermes/.env` as `SLACK_USER_TOKEN`
- **GitHub**: the `gh` command, logged in with `gh auth login`

To hand work to an agent, turn on Hermes' Kanban board too.

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
top of the page.

To change how it works, just ask Hermes, for example "read Slack only after an hour of silence".

A few commands for the rest (the installer puts `todo-manager` in `~/.local/bin`):

```bash
todo-manager status     # is everything working?
todo-manager pause      # stop for a while
todo-manager resume     # start again
todo-manager export     # save a readable copy of all your tasks
```

`todo-manager uninstall` stops it for good; your data and Notion pages are kept.

## Good to know

- **Where does my data go?** Your tasks are kept on your machine and in your Notion. New
  messages go only to the AI model your Hermes uses, and the model runs only when there is
  something new.
- **Will it undo my changes?** No. A field you change in Notion keeps your value.
- **Will an agent email people on its own?** An agent you hand a task to sends email only after
  you approve it in a comment. This is an instruction the agent follows, not a technical block.
- **What if the AI gets it wrong?** It will sometimes. Every message it leaves out is kept with
  a reason; tell it what it got wrong and it adjusts.
- **Who is it for?** One person on one machine, in English or Simplified Chinese.

## Learn more

- **Using it:** [Web UI](docs/web-ui.md) · [Notion pages and fields](docs/notion-schema.md) ·
  [Command line](docs/cli.md) · [Upgrades, backups and troubleshooting](docs/operations.md)
- **Other AI agents** (Claude Code, Codex, Cursor…) can read and change your tasks through the
  [API, an MCP server or a skill](docs/api.md):
  `npx skills add ControlNet/todo-manager`
- **Under the hood:** [How it works](docs/architecture.md) ·
  [Known limitations](docs/limitations.md)

## License

[AGPL-3.0](LICENSE)
