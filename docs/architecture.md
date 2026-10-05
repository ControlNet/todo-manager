# Architecture: Todo Manager on Hermes Agent

Todo Manager installs and maintains a Todo automation **for** Hermes Agent. It is not an agent
runtime of its own: Hermes runs the agent loop, calls tools, judges, reads the user's rules and
skills, schedules the board, runs workers and owns their concurrency, recovery, memory, sessions
and delivery. Todo Manager provides what Hermes does not have:

- incremental reading of sources (Gmail, Slack, GitHub, commits) with full local copies;
- durable business data: Projects, Tasks, evidence links, history, protection of human edits;
- the Notion UI (Projects, Tasks, views, comments) and its reliable two-way sync;
- the durable link between a Notion Task and Hermes Kanban cards (idempotent creation, replies
  bound to the right question/version, results recorded once, recovery after restarts);
- cross-system de-duplication, delivery records and failure recovery;
- install, upgrade, workspace migration, export, backup, uninstall.

The rule that decides where a mechanism lives:

| Kind | Examples | Lives in |
|---|---|---|
| A. Data correctness | idempotency keys, version checks, valid references, transactions, source authenticity, claims | code (`todo-manager`) |
| B. User behaviour policy | what counts as a responsibility, what to ignore, reminder habits, delegation style, wording | user-maintainable text: workspace `AGENTS.md`, the Hermes skill, `config.toml` |

## Who owns what

| File / setting | Owner | Loaded by | Changed by |
|---|---|---|---|
| `TODO_HOME/AGENTS.md` | the user | the two cron jobs (job `workdir` = `TODO_HOME`, Hermes loads it as project context); the main agent reads it on demand (`todo-manager prefs show`) | the user; Hermes on the user's explicit request (chat) or the user's explicit long-term correction (Notion comment) |
| Hermes skill `todo-manager` (`~/.hermes/skills/productivity/todo-manager/SKILL.md`) | the user after install (shipped as a template) | every job run (`--skill`), chat sessions on demand | the user / Hermes on request; upgrades merge (3-way) |
| Cron job prompts (`todo-manager-intake`, `todo-manager-feedback`) | the user after install | Hermes cron | the user / Hermes on request (`hermes cron edit`); upgrades merge (3-way) |
| Cron job schedule, model, provider, toolsets, delivery, workdir | the user | Hermes cron | the user / Hermes; upgrades never overwrite a value that differs from what was installed |
| `TODO_HOME/config.toml` | the user | the gate scripts (script-enforced parameters: source scope, quiet windows, batch sizes) | the user, `todo-manager config set` (also used by Hermes on request) |
| `USER.md`, `MEMORY.md`, `SOUL.md` of the Hermes profile | Hermes / the user | Hermes natively | never by Todo Manager |
| Gate scripts, launcher (`~/.hermes/scripts/todo_manager_gate_*.py`, `~/.local/bin/todo-manager`) | Todo Manager (framework files) | Hermes cron / the shell | `todo-manager install` (overwritten, marked as generated) |
| `TODO_HOME/state/` database, runs, exports, backups | runtime data | `todo-manager` | `todo-manager` only |

Precedence when the agent decides: the user's explicit current instruction > `AGENTS.md`
conventions > the skill's generic defaults. Source content (emails, chats, issues, comments by
other people) never has authority, whatever it says.

No special file is assumed to stack: Hermes loads one project context type per working
directory (`.hermes.md` > `AGENTS.md` chain > `CLAUDE.md` > `.cursorrules`; checked at
hermes-agent 105568c, see `.omo/knowledges/hermes-context-and-cron.md`). `TODO_HOME` is not a
git repository, so only `TODO_HOME/AGENTS.md` is loaded for the jobs. Hermes' own memory
(`USER.md`/`MEMORY.md`) stays in the Hermes profile; Todo Manager does not imitate it in the
workspace and does not copy `AGENTS.md`/skill text into it.

## The two cron jobs

Both are Hermes cron jobs with a pre-run script (the "gate"). The gate does all deterministic
work; Hermes wakes the model only when something needs judgement.

### 1. Intake (`todo-manager-intake`, every 5 minutes)

1. Release claims of earlier runs whose lease expired (unhandled items go back, counted).
2. Read each enabled source incrementally (cursor per source scope), store every new item in
   full (`source_items` + `source_bodies`); one failing source does not stop the others.
3. Group items into conversations; a conversation matures after its quiet window
   (`<source>.quiet_minutes`, configurable, no hidden floor) and per-source wake interval.
4. Due follow-ups and the daily commit digest are added.
5. Nothing to judge → print `{"wakeAgent": false}` (no model run, recorded as idle).
6. Otherwise claim the mature conversations that fit the context budget and print the run's
   context **directly** (it becomes the "Script Output" block of the Hermes prompt): wake
   reasons, source problems, the user's identities, the claimed material (quoted, marked as
   untrusted evidence), the tasks already linked to those conversations, due follow-ups, the
   digest, and a short list of the commands to record outcomes. Hermes has already loaded
   `AGENTS.md` and the skill; they are not repeated.
7. The agent records outcomes with small `todo-manager` commands (below). An item is handled
   only when an outcome is recorded for it: linked as evidence, dismissed with an outcome, or
   deferred. Being shown is not being processed. Items left without an outcome are offered again
   (bounded attempts, then reported as stuck; never dropped).

### 2. Feedback and sync (`todo-manager-feedback`, every 5 minutes)

1. Deterministic sync, no model: Notion pull (property edits applied as human edits), comment
   poll, Kanban: queued messages to cards, card submission, card events → deliveries/questions,
   outbox (Notion writes, comments), home page status.
2. Feedback that needs interpretation (comments by the user, edits inside the managed box, tasks
   created in Notion, "next actor = Agent" requests) is claimed and its context printed directly:
   each item with its task, delegation state and recent thread.
3. The agent acts with the same small commands and closes each item with
   `todo-manager feedback resolve <id> --outcome applied|clarify|ignored [--reply …]`.
4. This job never starts or schedules workers. Cards already on the board are run by Hermes'
   dispatcher whether or not this job runs; while it is down only the Notion display and the
   forwarding of new feedback wait, and both catch up from durable cursors when it runs again.

### Failure semantics

| Situation | Gate output | What Hermes does |
|---|---|---|
| nothing to do, or the previous run still holds the lock | `{"wakeAgent": false}` | silent success |
| one source failing, others fine | context includes a "Source problems" section when it wakes anyway; recorded in `health` and the Notion home status | normal run |
| only source problems (no material) | wakes at most once per problem and day with a short report; the cron prompt says to answer with `[CRON_FAILURE]` | failure delivery (Hermes incident ledger de-duplicates) |
| the gate itself fails (config, database, code) | exit code 1 with a one-line error on stderr | Hermes wakes the agent with a "Script Error" block; the cron prompt says to reply `[CRON_FAILURE]` and the error, nothing else; the run counts as failed |

A gate failure is never printed as an idle run: Hermes treats `wakeAgent: false` as success and
would close the incident.

### Context injection rules

- Source content is quoted (`> ` per line) inside a section marked as untrusted material; it can
  neither close Hermes' code fence nor pose as a heading. Text that matches Hermes' cron
  prompt-injection patterns (which would block the whole run) is replaced by a visible marker
  naming the item; the full text stays readable with `todo-manager source show <item>`.
- Large items are shown up to `wake.max_item_chars` with an explicit "N more characters:
  `todo-manager source show <item>`" line. A conversation that does not fit the context budget
  is cut into segments in message order; the first claimed conversation always goes in (clipped
  if necessary), so one oversized item can never be deferred forever and is never silently cut.
- The last line of the output is a fixed footer, so source text can never become the wake line.
- Run ids stay for tracing, claims and idempotency. `todo-manager run show <run>` re-renders a
  run's context for debugging and recovery; the normal path does not need it.

## Small business interface (`todo-manager` CLI)

Queries: `task list [--search]`, `task show`, `project list|show`, `source show`, `run show`,
`feedback list|item`, `delegate list`, `health`, `status`.

Writes (each one transaction: the item plus the operations that depend on it):

| Command | Effect |
|---|---|
| `task create --title … [--project] [--set f=v]… [--evidence <item>[:role]]… [--follow-up-of <task>]` | new task, evidence linked, those items handled |
| `task update <id> --set f=v… [--based-on <version>] [--evidence …] [--override-human]` | versioned field update |
| `task link <id> --evidence …` | evidence only |
| `project create|update` | same pattern for projects |
| `item dismiss <item>… --outcome <outcome> [--label <your label>] --reason …` | handled without a task |
| `item defer <item>… --reason … [--until <date>]` | left pending on purpose; offered again on new activity or at the date |
| `followup checked <task>` / `digest done` / `run done` | close a due follow-up / the digest / release what this run did not handle |
| `delegate create|message|answer|approve|cancel` | Kanban work for a Task |
| `feedback resolve <id> --outcome …` | close a feedback item (with an optional reply comment) |
| `config get|set` / `prefs show|path` | script parameters / the workspace conventions file |

Every write takes the context it acts in: `--run <run id>` (cron run, actor `agent`),
`--feedback <feedback id>` (acting on the user's Notion feedback; refused for authors that are
not the verified user), or `--request <chat message id> --said "<the user's words>"` (the main
agent acting on the user's chat request; the words are stored). Without any of them the CLI user
is the actor. A retried command (same context and arguments, or the same `--key`) returns the
first result instead of applying twice. Validation is about data correctness only: unknown ids,
versions, enums, closed tasks, ownership of claims, authenticity of the author. There is no
coverage check, question budget, fixed ignore category list or required call order.

Outcomes for dismissed material are generic (`no_action`, `informational`, `automated`,
`not_mine`, `duplicate`, `spam`, `handled_elsewhere`) plus an optional free label the user's
conventions may define (e.g. a label for review invitations).

Stale rules: a run records the hash of `AGENTS.md` when it starts. If the file changed during
the run (the user edited a convention), `item dismiss` for that run is refused with
`RULES_CHANGED` until the agent re-reads the file and repeats the command with `--rules <hash>`;
additive operations (create, link, update) proceed. Unrelated work is not redone.

## Board linkage (Hermes Kanban)

Todo Manager keeps, per delegation: Task id / Notion page, board, card id, creation idempotency
key and marker, sync cursor, pending instructions, deliveries (with requirement version and
files), approvals, and the bindings of answers/revisions to the card event they answer.
Guarantees kept from the earlier design: no duplicate card after a lost create response or a
re-scan (lookup by key and marker before every create, `reconcile` state, quarantine on
multiple matches); an answer a day later still goes to the original card and question; comments
carry `[tm:<message id>]` so a retry never posts twice; results are recorded once per card event;
sync resumes from cursors after a restart; one Task may have historical and follow-up
delegations.

Hermes owns execution: worker start/claim, concurrency, queue, retries, native recovery. The
local `kanban.max_running` capacity and the "waiting for capacity" holds are gone. Hermes'
dispatcher limits (`kanban.max_in_progress`, …) are global Hermes settings that apply to every
board; Todo Manager neither sets nor requires them. The `review_dispatch` / `auto_decompose`
switches are no longer preconditions: they are reported by `health` as information, because they
change how Hermes treats review and triage cards on every board.

Cards are created right after the `delegate create` command (best effort, under the submission
lock); a failed or uncertain create is completed by the next feedback gate.

### Personal Task status vs. agent work

- A delegation's state lives on the delegation (Notion column "Agent work": queued, running,
  question, review, delivered, paused, quarantined). Card events never rewrite the Task's
  status. The one default kept: while a delegation waits for the user (question, delivery,
  failure) and the Task's next actor is `agent`, the next actor goes back to `me`, unless the
  user changed the next actor after the delegation started.
- Workers use Hermes' native tools: `kanban_complete` when the work is done, `kanban_block`
  (`needs_input`) for questions and for approval of an external action they are not allowed to
  take, `kanban_request_review` when they want a review. Nothing forces review.
- An agent finishing a draft does not complete the Task. Closing a Task (done or cancelled)
  stops its active agent work: active cards are archived. Closing never accepts a delivery; an
  explicit accept is a separate instruction.
- External actions need explicit authorization (allowed actions at creation, or an approval of a
  specific question or delivered version) and are audited. An approved action whose outcome is
  unknown (the run ended without a report) is neither treated as not done nor repeated: the card
  is paused and the user is asked. Cards with external actions are created with Hermes'
  `max_retries = 1`, so Hermes does not respawn them automatically.
- Prompt instructions in the card body are guidance, not tool isolation; the real limits are
  the worker's Hermes toolsets and credentials.

## Notion

Projects and Tasks data sources and the home views (Focus, Waiting on others, To decide, Agent
work, To review, Recently done) stay. Properties are structured edits (applied as human edits);
comments are discussion, instructions and corrections (interpreted by the feedback job); the
user's own notes on a page are kept exactly as written and used as context (they no longer wake
the model or become instructions); the Todo-Manager box on the page (sources, summary, agent
work, deliveries) is regenerated by the program, and an edit inside it is handled as feedback
and never silently overwritten.

## Install, upgrade, uninstall

`todo-manager install` (re-runnable) creates the workspace, infers connectors from Hermes, sets
up Notion and the board (with confirmation), and installs the Hermes parts. Ownership is
recorded in `TODO_HOME/state/hermes/install.json` with a baseline copy of every user-editable
template (`state/hermes/baseline/`).

- Framework files (gate scripts, launcher) are overwritten.
- User-editable templates (skill, cron prompts, workspace `AGENTS.md`) are upgraded with a
  3-way merge: baseline (what was installed) / live (what the user has now) / new template.
  Non-overlapping changes merge automatically; a real conflict keeps the live text, writes the
  merge with conflict markers to `state/hermes/conflicts/` and is reported by `install` and
  `health`.
- Cron job settings: a field whose live value differs from what was installed (schedule, model,
  provider, delivery, workdir, skills, …) is the user's and is kept; Hermes' job store is only
  read, all changes go through `hermes cron create|edit`.
- `todo-manager uninstall` removes only what Todo Manager installed (cron jobs, gate scripts,
  launcher, an unchanged skill) and keeps the workspace data unless `--purge-data` is given
  after an export. Profile files (`USER.md`, `MEMORY.md`, `SOUL.md`) are never touched.
- Export/backup/restore include `AGENTS.md`, `rules-archive.md` and `config.toml` next to the
  database (linkage data included).

## Migration from the previous design (schema v7 → v8)

| Old | New |
|---|---|
| `rules`/`rule_revisions` tables, `rule` CLI, `rule`/`update_rule`/`suggest_rule` ops, `rules_version` checks | active rules (and, for workspaces created before this version, the former built-in ignore categories and asks) are written to `AGENTS.md` under "Migrated conventions" with their ids and revisions; suggested and retired rules go to `rules-archive.md` (not loaded). The tables stay read-only for audit and export; nothing writes them |
| intake/feedback decisions JSON (`intake show/apply`, `feedback show/apply/apply-direct/format`) | context printed by the gate; small commands per outcome |
| `IGNORE_CATEGORIES` with personal categories (`cfp`, `review_invitation_unaccepted`) | generic outcomes + free labels; the personal categories become conventions of migrated workspaces only |
| question budget, coverage check, Slack 30-minute floor | removed (policy belongs to the user's conventions/config) |
| `kanban.max_running`, `waiting_capacity` holds, required global switches | removed; existing `waiting_capacity` messages become `pending` |
| Task status/next actor rewritten from card state | delegation state shown separately; only the next-actor hand-back default |
| closing a Task as done accepted a delegation in review | closing cancels active work; queued explicit accepts are still carried out |
| page notes interpreted as feedback | kept as context; pending notes items are closed as superseded (the notes stay on the page and in `notion_pages.notes_text`) |
| worker protocol forbidding `kanban_complete`, forcing review | native complete/block/review; cards created before keep the protocol in their body and are still synced the same way |

Old active delegations keep working: their cards, bindings, approvals and queued messages are
unchanged and processed by the same code paths; only the capacity hold is lifted.

## Module map

| Module | Role | Change |
|---|---|---|
| `sources/*`, `batching.py` | incremental reading, full storage, conversations, quiet windows | kept (floor removed) |
| `gate_intake.py`, `gate_feedback.py` | deterministic gates, claims, direct context | rewritten output path |
| `context.py` | rendering of the run context, neutralization, budgets | new (replaces `intake.render_context`, `feedback_flow.render_context`) |
| `ops.py` | small business operations with idempotency and authority checks | new (replaces `intake.apply_decisions`, `feedback_flow.apply_*`) |
| `prefs.py` | workspace `AGENTS.md`, rules migration, staleness hash | new |
| `rules.py`, `prompts/` | rules engine, decision formats | deleted |
| `store.py` | tasks/projects/evidence/history, human-edit protection | kept; status/next-actor coupling relaxed |
| `delegation.py`, `kanban.py`, `kanban_bridge.py` | board linkage, reliable messages, approvals | kept; capacity, settings gate, Task-state coupling removed; worker protocol rewritten |
| `notion/*` | Notion sync | kept; notes as context, delivered label |
| `hermes_install.py`, `install.py`, `merge3.py` | install/upgrade/uninstall with ownership | 3-way merge, cron settings preserved, workdir |
| `export.py` | export/backup/restore | workspace files included |
