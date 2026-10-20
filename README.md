# Todo Manager

**Your AI agent reads your email, Slack and GitHub, and keeps your todo list in Notion for you.
When a task is something the agent can do, you hand it over with one drag.**

Todo Manager works with [Hermes](https://github.com/NousResearch/hermes-agent), the self-hosted
AI agent. It adds two scheduled jobs and a skill to your Hermes and uses the AI model and agent
board you already have there. Install it once, then you mostly just open Notion.

## What it does

- **Finds your todos for you.** "Could you review this before Friday?" in an email becomes a
  task in Notion: due Friday, with a link back to the email. Newsletters and threads you were
  only copied on are skipped.
- **Puts your decisions in one place.** Things only you can decide go to a *To decide* board. Drag
  the card to *Done*, *Cancel*, *My to-do* or *Assign to Agent*.
- **Lets the agent do the work.** Drag a card to *Assign to Agent* and a Hermes agent works on
  it: drafting a reply, collecting information, writing a document. If it needs something, it
  asks in a comment on the task. The result comes back as a page under the task.
- **Understands plain words.** Comment "the deadline moved to Monday" on a task, or tell it "from
  now on, ignore mail from this list", and it remembers.
- **Closes tasks by itself.** When you reply to the email or your pull request is merged, the
  task is marked done.

## What it looks like

A Notion page with a few lists and boards:

```
Todo Manager
  🤔 To decide        cards waiting for your call
  🤖 Agent work       what the agent is doing, and what it needs from you
  ⭐ Focus            what to do now: due soon, or starred by you
  📁 Active projects
  ⏳ Waiting on others
  📋 All open tasks
```

Each task page has a small box that Todo Manager keeps up to date (why it is your task, what
"done" means, links to the sources). Everything else on the page is yours: write notes there and
the agent reads them.

## Your data and your say

- **Your edits stay.** A field you change in Notion keeps your value.
- **It reads your email, Slack and GitHub.** An agent sends an email only after you approve it
  for that task.
- **Your data lives on your machine**, in a local database. Tasks go to your Notion, and new
  messages go to the AI model your Hermes uses.
- **Low token use.** A small script checks for news every five minutes and calls the AI model
  only when there is something new.

## What you need

- A Linux machine (a home server or a small cloud VM is fine) with
  [Hermes](https://github.com/NousResearch/hermes-agent) installed and working. Tested with
  Hermes 0.21 and 0.22.
- Python 3.11 or newer. Nothing else to install.
- A free Notion account.
- At least one source:
  - **Gmail**: the Gmail account your Hermes already uses (through himalaya);
  - **Slack**: a Slack user token saved in `~/.hermes/.env` as `SLACK_USER_TOKEN`;
  - **GitHub**: the `gh` command, logged in (`gh auth login`).
- Optional, for *Assign to Agent*: Hermes' Kanban board.

## Get started

**1. Connect Notion (about 3 minutes)**

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and create an
   internal integration. Turn on *Read content*, *Update content*, *Insert content*, *Read
   comments* and *Insert comments*.
2. Copy its secret and add it to Hermes' settings file `~/.hermes/.env`:

   ```bash
   echo 'NOTION_API_KEY=paste-your-secret-here' >> ~/.hermes/.env
   ```

3. In Notion, create an empty page (for example "Todo Manager"). Open its `···` menu →
   *Connections* and add your integration.

**2. Install**

```bash
git clone https://github.com/ControlNet/todo-manager.git
cd todo-manager
./bin/todo-manager install
```

The installer finds most settings in your Hermes setup: language, time zone and which of Gmail,
Slack and GitHub are connected. It asks only a few questions (which Notion page to use, which
mailboxes and chats to read). It shows every change before making it, and you can run it again
at any time.

Keep the downloaded folder where it is: Todo Manager runs from it.

**3. Open your Notion page**

Once the installer has started Todo Manager (it asks before it does), tasks from your recent
conversations appear within a few minutes.

## Everyday use

You mostly live in Notion: drag cards, write comments, add notes. A few commands help now and
then (the installer puts `todo-manager` in `~/.local/bin`):

```bash
todo-manager status     # is everything working?
todo-manager pause      # stop for a while (holiday, debugging)
todo-manager resume     # start again
todo-manager export     # save a readable copy of all your tasks
todo-manager uninstall  # stop for good; your data and Notion pages are kept
```

You can also just ask Hermes in chat, for example "read Slack only after an hour of silence".

If something stops working (a mailbox that no longer syncs, an agent that got stuck), it shows up
at the top of your Notion page and in `todo-manager status`.

## Good to know

- **The AI makes mistakes** about what is yours. Every message it skips is kept with a one-line
  reason. Tell it what it got wrong and it learns your preferences.
- **Built for one person on one machine.**
- **Languages:** English and Simplified Chinese.
- **Optional extras:** Gmail labels that show each mail's project and task state, and instant
  sync (seconds instead of minutes) through a small background service. See
  [operations](docs/operations.md).

## Learn more

- [Operations guide](docs/operations.md): install details, upgrade, backup, troubleshooting
- [Notion pages and fields](docs/notion-schema.md)
- [Command line reference](docs/cli.md)
- [How it works](docs/architecture.md) and [known limitations](docs/limitations.md)

## License

[AGPL-3.0](LICENSE)
