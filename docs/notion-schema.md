# Notion structure and mapping

Todo Manager keeps its own SQLite database as the source of truth. Notion is the place where
you read and edit: a home page with eight sections, a **Projects** database and a **Tasks**
database. `todo-manager install --json` previews the structure and upgrades before confirmation.

Names below are shown in English and Simplified Chinese. The language is chosen once at
installation (from `language` in `config.toml`) and stored in the local state, so changing
the configured language later does not rename anything in Notion; option names of every
supported language are understood when reading your edits.

## Setup

Create an empty top-level page and connect the Notion integration, then run
`todo-manager install`. The installer selects/adopts that page, previews the changes,
and creates missing databases and views after confirmation. Re-running install upgrades
owned structure and preserves custom content. Property/data-source/view IDs and structure
language are retained, so renaming a column does not break synchronization.

## Tasks

| Field | Type | Edited by | en | zh-CN |
|---|---|---|---|---|
| `title` | title | you | Task | 任务 |
| `status` | select | you | Status | 状态 |
| `decision` | select | you | Decision | 决定 |
| `next_actor` | select | you | Next by | 下一步由谁 |
| `project_id` | relation → Projects | you | Project | 项目 |
| `next_step` | text | you | Next step | 下一步 |
| `waiting_on` | text | you | Waiting on | 等待对象 |
| `due_date` | date | you | Due | 截止日期 |
| `follow_up_date` | date | you | Follow up | 跟进日期 |
| `snooze_until` | date | you | Snoozed until | 暂缓至 |
| `focus` | checkbox | you | Focus | 精选 |
| `change_note` | text | you | What changed | 变化 |
| `delegation` | select | Todo Manager | Agent work | 委派 |
| `agent_progress` | select | Todo Manager; you drag Done / My to-do / Cancel | Agent progress | Agent 进展 |
| `source` | select | Todo Manager | Source | 来源 |
| `tm_id` | text | Todo Manager | TM ID | TM ID |
| `updated` | last edited time | Notion | Updated | 更新于 |

"Edited by you" fields are imported back as your edits (they win over later agent
judgements, see below). Fields owned by Todo Manager are rewritten from local state; a manual
edit to them is reverted on the next push.

### Status and next actor

| Local value | en | zh-CN | Meaning |
|---|---|---|---|
| `todo` | To do | 待办 | Open, nobody working on it yet |
| `in_progress` | In progress | 进行中 | Being worked on (by you or an agent) |
| `waiting` | Waiting | 等待 | Someone else acts next; `waiting_on` + `follow_up_date` |
| `decision` | To decide | 需要决定 | A real choice only you can make |
| `review` | To review | 待我验收 | Something for you to review (agent work shows its own state in "Agent work") |
| `done` | Done | 完成 | Closed: fulfilled |
| `cancelled` | Cancelled | 取消 | Closed: not needed |

Next actor: `me` Me / 我, `agent` Agent / Agent, `external` Others / 外部.

The pair is normalised so it can never contradict itself: `waiting` always means next actor
`external` (and choosing "Others" makes the task `waiting`); `decision`, `review`, `done` and
`cancelled` mean `me`; `todo` / `in_progress` go with `me` or `agent`. Snoozing (`snooze_until`)
is independent of the status. A real external deadline (`due_date`, with its basis recorded in
the page body) is separate from a suggested follow-up date; deadlines are never invented.

Setting "Next by" to Agent on a task without active agent work asks the model (once) to turn
it into a delegation or ask a question. The status is yours: agent work never changes it.
Delegating sets "Next by" to Agent; while the work waits for you (a question, a delivery, a
failure) "Next by" goes back to Me, and to Agent again when the work resumes, unless you set
"Next by" yourself after delegating. Closing a task cancels its active agent work; it never
accepts a delivery. Moving a task to the trash cancels it; restoring it reopens it with its
previous status.

### Agent work and source

Agent work (`delegation`): `queued` Queued / 排队中, `running` Running / 执行中, `blocked` Needs my
answer / 等我回答, `review` To review / 待我验收, `delivered` Delivered / 已交付 (the latest agent
work ended with a result; the task stays open until you close it), `paused` Paused / 已暂停,
`quarantined` Problem / 异常. A cancelled delegation, or one that ended without a result,
clears the column; so does handling the delivery (accepting it, or Done / My to-do on the Agent
progress board).

Agent progress (`agent_progress`), the column of the Agent work board: `answer` Needs my answer /
等我回答 (a question, also on a hand-over the run did not start), `review` To review / 待我验收 (a
version under review or a delivered result not yet handled), `working` In progress /
处理中 (a hand-over not yet processed, queued, running, paused or a problem: "Agent work" tells
which), and the user's drags `done` Done / 已完成, `todo` My to-do / 设为待办,
`cancel` Cancel / 取消. Empty when the task has no agent work for you to see.

Source: Email / 邮件, Slack, GitHub (issues, PRs and the commit digest), Notion, Matrix, Manual /
手动, Migrated / 迁移.

## Projects

| Field | Type | Edited by | en | zh-CN |
|---|---|---|---|---|
| `title` | title | you | Project | 项目 |
| `status` | select | you | Status | 状态 |
| `domain` | select (free options) | you | Area | 领域 |
| `goal` | text | you | Goal | 目标 |
| `next_step` | text | you | Next step | 下一步 |
| `blockers` | text | you | Blockers | 阻碍 |
| `recent_activity` | text | you | Recent progress | 近期进展 |
| `repos` | text (comma separated) | you | Repositories | 仓库 |
| `recent_activity_at` | date | Todo Manager | Progress date | 进展时间 |
| `tm_id` | text | Todo Manager | TM ID | TM ID |
| `updated` | last edited time | Notion | Updated | 更新于 |

Project status: `active` Active / 进行中, `paused` Paused / 暂停, `done` Done / 完成, `archived`
Archived / 归档. The task relation adds a back-relation column "Tasks" / "任务" to Projects.

Each project page has an icon, so a project is easy to spot: Notion shows it next to the
project's name in every task view. A new project gets a built-in Notion icon in a colour no
other project uses (`project create --set icon=<name>:<color>`, choices from `project icons`).
The page icon is yours to change: an icon, colour, emoji or image you pick in Notion is kept and
never written over. Stored in `projects.icon` (schema 14) as `name:color`, an emoji,
`custom_emoji:<id>`, `external:<url>`, or `file` (an uploaded image, not written back).

## Home page

| Section (en / zh-CN) | Linked view | Filter |
|---|---|---|
| 🤔 To decide / 需要决定 | board by decision | status to decide or a recorded decision |
| 🤖 Agent work / Agent 工作 | board by Agent progress | any Agent progress |
| ⭐ Focus / 精选待办 | table | open (to do, in progress), next by me, not snoozed, and focus or due within a week (agent work is on the board) |
| 📁 Active projects / 活跃项目 | table | project status active |
| ⏳ Waiting on others / 等待别人 | table | waiting, not snoozed; sorted by follow-up date |
| 📋 All open tasks / 全部未完成任务 | table | every task except done/cancelled |
| 🕘 Recently done and notable changes / 最近完成与重要变化 | table | edited in the last week and closed, or with a change note |

At the top, a small **data freshness** callout ("Data freshness (maintained by Todo Manager)" /
"数据新鲜度（由 Todo Manager 维护）") shows "all sources are syncing normally" or a short list of
problems (stale sources, given-up writes, quarantined agent work, failed feedback) plus the
hour of the last check. It is rewritten only when its content changes; logs never go to Notion.

`todo-manager export` writes the same sections, computed locally, to `views/home.md`.

Each section's heading is its title. Every linked view also shows the source database title
("Tasks" / "任务") above it; the Notion API cannot hide it, so hide it in Notion itself (the
view's `···` menu, "Hide database title"). The API cannot move existing blocks either: the order
above applies to new home pages, and an existing page is reordered by dragging. Neither changes
synchronization, which follows view IDs, not block positions.

Views made by an earlier version (Focus before 2026-10-12's agent board is current again) are updated by
`todo-manager install`, after your confirmation, only while their filter is still the one Todo
Manager created; a view you changed or removed is left alone and `install` says what to add.

## Task page body

Each task page has ONE callout maintained by Todo Manager, marked "Current brief (maintained by
Todo Manager; write your own notes outside this box)" / "当前说明（由 Todo Manager 维护；手写笔记请
写在此框外）". It holds why the task is yours, done criteria, background, due-date basis, why it
is in focus, sources (one line per link, e.g. per mail thread, dated by its latest message), agent
work and the latest delivery (collapsed). URLs in the text are written as links. Everything outside
the callout is yours and never touched: your notes there are context. They are kept exactly as
written and shown with the task to the agent (in the feedback run and in a new agent card); a
change of them wakes nothing and is never an instruction by itself. To ask for something, write
a comment.

The callout is replaced with a search-and-replace of the exact text read just before, so
concurrent edits elsewhere survive. If you edit text inside the callout, the edit is held and
handed to the model as feedback instead of being overwritten; afterwards the box is regenerated.
If you delete the callout, it is re-inserted at the top. Only the visible text counts as an edit:
Notion turning a bare URL or domain into a link is not one, and neither is changing only where a
link inside the callout points.

## Delivery pages

Each delivered version of agent work gets its own page under the task page, "交付 v1 · 10-12 17:28"
/ "Delivery v1 · …" (📦), so it can be read in Notion without access to the server. In order:
the delivery summary and the agent's notes; under "PDF 与图片" / "PDFs and images" the PDFs and
images, uploaded and shown inline (Word, PowerPoint, Excel and CSV as downloadable files); each
text file (`.md` as formatted text, `.txt` and `.eml` in a copyable box); and "仅保存在服务器上"
/ "Kept on the server only": the agent's own records (JSON, archives, patches), missing files,
and files over the workspace upload limit (5 MB on a free workspace). The agent box on the task
page shows the latest summary and links to the version's page. Pages are created by the
`push_delivery` outbox op (state in `delivery_pages`, schema 15); existing deliveries get theirs
on the next sync.

## Comments

Your comments on task pages are feedback: supplements, answers to agent questions, revision
requests, approvals, corrections or conventions. Replies from Todo Manager go into the same
discussion. Comments by the integration itself are never treated as feedback, and comments
by other people are information, not instructions.

## Sync rules

- Import runs every 5 minutes (feedback gate). Pages are queried by last edited time with an
  overlap window; each page's editable values are compared with what Todo Manager last wrote
  or saw, so only real human edits are imported (our own writes never echo back).
- Push is read-before-write: unseen human edits are imported first, then only the differing
  properties are patched. A page create whose response was lost is reconciled by looking up
  the `TM ID` before any retry.
- Unknown option names (for example a status you typed yourself) are reported in
  `todo-manager health`, not imported.
- Agent/program writes carry the version they were based on; a field you changed after that
  version is never overwritten. A task you closed is never reopened automatically; genuinely
  new responsibility becomes a linked follow-up task.

## Decision-board update (schema 11)

Setup and upgrades use `todo-manager install`; the older `notion setup/plan` commands in the
historical description above are no longer public CLI entry points. The homepage now also
contains **All open tasks / 全部未完成任务**, without focus or due-date filtering.

**Decisions / 需要决定** is a board grouped by editable **Decision / 决定**:
Pending / 待决定, Cancel / 取消, Done / 已完成, My to-do / 设为待办, Assign to Agent / 交给 Agent
(choices since 2026-10-12; boards set up with Agree / Decline get them at the next `install`,
which keeps an old option while a task still has it). Cards expose the title, next step/background,
due date and project. Every option stays visible even if empty, so Notion also shows its own
"No Decision" column, always empty here: the public API cannot hide one column without hiding the
empty choice columns as well, so hide it once in Notion (column `···`, "Hide").

Cancel, Done and My to-do are applied by the import itself (the status changes; a status set in
the same edit wins; Cancel and Done record "chosen on the decision board" as the closing reason).
Assign to Agent sets the status to-do and next by Agent and queues a delegation request for the
feedback run. In every case the choice is then cleared, so the handled card leaves the board.
A task handed to the agent is on the Agent work board at once: In progress until the run has
started agent work, or Needs my answer when the run asked a question in a comment instead
(answering there puts it back to In progress). Asking for agent work in a comment on the task does
the same without a drag: once the run starts the work, the card leaves the To decide board.
Moving a card never sends RSVPs or other external messages. The older Agree / Decline choices
are still read and go to the feedback run as before.

**Agent work / Agent 工作** is a board grouped by **Agent progress / Agent 进展**: Needs my answer /
To review / Done / My to-do / Cancel / In progress. Todo Manager writes the state columns; you
drag a card to one of the three choices, applied by the import itself without a model run:

- **Done**: accepts the version under review (the card completes, it is not cancelled) and closes
  the task as done ("chosen on the Agent progress board").
- **My to-do**: accepts the version under review, stops agent work still going (you take the task
  over), and makes the task your to-do.
- **Cancel**: cancels the task and its agent work.

The card then shows its new state (or leaves the board); a card dragged to a state column goes back.
Answers to the agent's questions and change requests are comments on the task page: the
feedback run sends them as an answer or a revision. Like the decision board, every column stays
visible, so hide Notion's empty "No Agent progress" column once.

Installs set up before 2026-10-12 had a board grouped by Agent work and a separate "To review /
answer" table, and Focus also listed agent work waiting for you. `todo-manager install` (after
confirmation; snapshot in `backups/notion-agent-board-v1.json`) adds the select, creates the new
board under the renamed heading, moves the old board and the To review section to the Notion
trash, and narrows Focus when it is still as created.

Pending creation requests also receive a new feedback identity when their decision changes;
the original body completeness requirement remains in force. Cancelling a task in the same
edit as assigning Agent takes precedence and does not create a delegation request.
