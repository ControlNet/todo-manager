# Hermes-native architecture

Verified on 2026-10-06 against installed Hermes source `105568c1558e1cb494e6edf1a2998d524ebe809e`.
The original native architecture round started at `a820251`. The concentrated reliability
round started at `6b02612b5b19c11384664586ceb500f1e49983e8`; fetched `origin/main` matched it.
See [R01–R16 reliability changes](reliability-audit-2026-10-06.md) for the current recovery contracts.
See [native verification](hermes-native-verification.md) for executable evidence and limitations.

## Running pipeline

```text
Gmail / Slack / GitHub
  -> intake cron script: incremental fetch, pagination, full-text retention, maturity, claims
  -> idle: {"wakeAgent": false}; work: bounded materials directly on stdout
  -> Hermes: native skill + stored intake prompt + project context + profile memory
  -> Todo CLI: Task/Project/evidence/outcomes -> outbox -> Notion

Notion edits, comments, next_actor=Agent
  -> feedback cron script: import human edits, record delegate_request, sync cards/outbox
  -> Hermes feedback agent: interpret the request, prepare concrete delegated instructions
  -> Todo CLI: persistent delegation + idempotency key + Task/page/version linkage
  -> native ready card, or initial blocked staging when a notification route is configured
  -> register native passive notification subscription -> native unblock
  -> Hermes dispatcher -> profile worker in a native task workspace
  -> native card events -> feedback script -> Notion questions, partial/aggregate results
  -> Notion discussion reply -> bound feedback/delivery/card/event -> native comment + unblock
```

There are exactly two scheduled agent entry points. Feedback owns delegation and result sync.
No delegation cron, execution cron, model client, dependency executor or notification service is
introduced. The optional real-time sync daemon ([realtime-sync.md](realtime-sync.md), off by
default) is the one resident process: it runs the feedback job's deterministic sync steps on
Notion webhook events, local database changes and board changes, never a model, and asks Hermes
to run the feedback job when feedback is waiting; while it is healthy the gate only claims. Source quiet windows and minimum model wake intervals are
independent of cron schedules. Waiting on a person or a future retry does not by itself wake a model.

## Responsibilities

| Responsibility | Hermes | Todo Manager |
|---|---|---|
| Reasoning, tools, model loop | Native agent | Supplies material and validates business writes |
| Scheduling and pre-run scripts | Native cron, Script Output/Error | Deterministic intake/feedback scripts |
| Prompt and context loading | Native cron skills, stored prompt, workdir discovery, profile memory | Deploys owned templates via cron CLI |
| Work orchestration | Kanban dispatcher, claim lock, profiles, decomposition, dependencies, retry/review | Task/delegation/card associations and idempotent messages |
| Source ingestion | Existing provider/tool ecosystem | Incremental readers, pagination, dedupe, cursors, full text, health |
| Notion | Agent understands user requests | Property/body/comment sync, human edit protection, outbox |
| Notifications | Cron deliver/failure_deliver; native subscriptions and gateway notifier | Explicit route registration and missing-subscription repair |
| Recovery | Worker claims, native retry and channel delivery checkpoints | Lost create/message response reconciliation; unknown external action holds |
| Persistence/export | Native profile and Kanban assets | Business database, framework snapshots, migration archives |

## Ownership and loading

| Surface | Owner | Actual loading and scope | Upgrade |
|---|---|---|---|
| Workspace `PREFERENCES.md` | User | Read afresh by both gates and printed into every woken intake/feedback context (first 8000 characters; refused cron phrases withheld) | Never written by the installer; edited directly by the user or by Hermes on the user's words |
| Dedicated Todo workspace `AGENTS.md` | Project | Hermes project discovery when cron has workdir; higher-priority HERMES.md / AGENTS.override.md may shadow it | Back up, then replace a known owned file |
| `hermes_assets/prompts/intake.md` | Project | Source template; installer copies text into native cron `prompt` | Native cron edit, not a runtime file loader |
| `hermes_assets/prompts/feedback.md` | Project | Same, including delegation and interaction workflow | Native cron edit; no delegation.md |
| `skills/productivity/todo-manager/SKILL.md` | Project | Native cron skills, command reference and constraints | Back up and replace when ownership is known |
| `<profile home>/memories/USER.md` | User/Hermes | Native profile preference snapshot at session start; shared by every session in that profile; not used for Todo conventions | Never managed by this installer |
| `<profile home>/memories/MEMORY.md` | Hermes/user | Native factual memory, enabled independently | Never managed |
| `<profile home>/SOUL.md` | User/Hermes | Native identity, separate from project discovery | Never managed |
| Cron schedule, deliver, failure_deliver, workdir, skills, model, provider, reasoning_effort, paused state | User | Native cron definition | Existing values preserved even when matching an old default |

Todo conventions (what becomes a task, what to ignore, when to ask, how to delegate) live in
the workspace `PREFERENCES.md`, the user's own file with scope headings (General, Intake,
Feedback, Delegation). The skill tells a chat session to edit it directly on the user's words
instead of using the memory tool, so the profile-wide USER.md capacity is not spent on Todo
rules and nothing in Hermes is migrated or changed. Feedback may edit it on a feedback item
written by the user; intake never does. Each woken run reads it again, so an edit applies from
the next run without a command. A running agent card does not see it: requirements for running
work go through card messages. The workspace contains no substitute MEMORY.md or SOUL.md, and
other personal preferences stay in native memory under its capacity, enable flags and write
approval. Explicit user corrections in chat are handled in that chat; no Matrix history scan is
scheduled.

Cron agents skip background memory/skill review in this version. Explicit memory tool calls
are a separate path. Todo Manager does not increase memory limits, evict entries or replicate
profile memory. Old rules and revisions remain in the database/archive for audit. The
preferences file is free text given to the model as written: there is no runtime DSL,
preference parser or approval gate. `prefs.file_hash`, `_rules_check`, `--rules`
and gate `rules_hash` writes have been removed. The old SQL column remains for historical
exports. Task versions, human field ownership, request idempotency and approval versions remain.

## Gates and failure semantics

A nonempty work context includes its materials, not just a run ID. Oversized inputs are marked
and give a full-text command; source bodies remain stored. Quoted source text is untrusted
material. Text matching the native injection scanner is marked with a full-text reference;
the scanner is not bypassed. No personal categories are encoded in gate branches.

A source failure is recorded per scope and exposed through health; other sources can continue.
Incident context is rate-limited. A gate exception exits nonzero. A normal agent cron can still
wake with Script Error; both prompts require no business changes and `[CRON_FAILURE]` in that
case. This is not a guarantee of native fail-fast. Successful empty script output is skipped
by `_build_job_prompt`; the explicit idle contract is `{"wakeAgent": false}`. Idle agent cron
runs do not deliver a summary. `[SILENT]` suppresses ordinary final-response delivery.

## Delegation and worker boundaries

A repeated Notion Agent property value records one delegate_request. The agent then decides
concrete instructions. A ready card assigned to a valid dispatchable profile is a normal
entry: triage/decomposition are optional. An invalid or missing profile is a submission error,
not running work. Blank profile configuration is not a general routing mechanism.

One active delegation per Task is enforced transactionally unless the existing explicit
parallel-work option is requested. Supplements/revisions use the active delegation. The
submission lock serializes lookup/create/link in Todo Manager; Hermes owns worker claim locks.
Lookup includes archived cards, so a lost response or a restart cannot create a second card
for the same delegation; a create that failed after its card was written (subscription or
parking) is reported as uncertain and looked up the same way. Multiple matches are quarantined.
Supplements pending at submission go into the card body; one queued while the create runs is
posted as a comment once the card is linked (a pause then blocks it). No native jobs/state
tables are written directly.

Cards carry Task/page/delegation references, requirement version, goals, context, deliverables,
authorization and the minimum approval/pause/result/unknown-action protocol. The protocol and
full-text commands precede potentially long content. Native worker context caps body at 8 KiB,
comments at 2 KiB and individual result fields at 4 KiB. `delegate show` returns complete stored
requirements; `task show` includes full page notes. Ordinary workers do not inherit feedback
chat history or its entire prompt. Their profile memory, native project context, workdir,
card skills, body/comments, parent results and attempt history are native inputs.

The decomposer/specifier uses a shorter 4,000-character body input and an auxiliary model,
not a full worker loop. Native children receive their own body and are not guaranteed to
inherit the root protocol, skills or authorization. Feedback instructions require copying
necessary constraints into children; this is agent guidance, not a native enforcement guarantee.

Scratch is the default native workspace. Coding work may use configured native worktrees;
ordinary `dir` workspaces provide no write isolation. Todo's control workspace is not a common
coding directory. Native decomposed worktree children omit an inherited path so dispatch can
materialize independent worktrees. Isolation does not prove semantic non-overlap: task scopes,
dependencies and integration ownership still matter.

## Children, questions, completion and cancellation

`delegation_children` and `delegation_child_events` store cross-system lineage/cursors and
local result history. Ownership is proved by creation-event `creator_task_id` or
`from_decompose_of`, recursively; dependency edges are never used as ownership. No child is
turned into a new Notion Task automatically. Child results are explicitly partial; root
results represent the delegation, and neither automatically completes the user's Task.

Child questions become delivery records containing the actual child card and event. Replies
in their Notion discussion recover that delivery ID; direct replies use `delegate answer
--question <delivery-id>`. Lost comment responses reconcile by marker before retrying.
A later question, a pause or cancellation prevents stale resumption. Changed requirements
void external authorizations only; a plain answer stays valid.
External approvals on child questions are explicitly refused by this adapter: the root can
perform the separately authorized external action using the existing version-bound protocol.
There is no automatic transfer of a root approval to a child.

Review belongs to Hermes' execution/review cycle. Human waiting uses blocked. A user acceptance
command only applies to its bound review version; native review workers can move it meanwhile.
For done cards, Todo Manager creates a new delegation on the same Task and persists predecessor
and previous-delivery IDs. It does not invoke review reopen on done or undo completed dependencies.

Cancellation archives the root first, then only proven owned descendants, observing lineage
again for children committed during termination. Native termination outcomes are checked.
Unstable lineage or unconfirmed termination remains unconfirmed. This is a bounded connector
operation, not a resident cancellation executor. An archive can release unrelated dependants;
those are not cancelled. Late results and unknown approvals remain recorded.

Pause/resume and ordinary supplement/revision messages currently target the entry card.
When children exist, pause posts an explicit Notion scope notice: descendants are not confirmed
paused. There is no atomic pause across blocked/todo/review/running child states in this adapter.
Use scoped native controls or cancellation of the proved ownership set. Do not promise that
modifying USER.md or notifying a chat wakes a dependency-blocked root.

## Notifications

Configure platform/chat/thread/notifier profile explicitly in `kanban.notify_*`. No route means
no registration and no sending. Routes are pinned to new delegations; migrated rows adopt
configuration once. Passive `notify` is the default; no fabricated session or automatic wake.
`api_server`, which requires wake-based delivery, is rejected by this passive adapter.

New routed cards are created with native `initial_status="blocked"`. After subscription
registration they are unblocked natively. A failed registration leaves a nondispatchable card;
recovery finds the same card, registers and only lifts the identifiable initial staging block.
It never lifts a worker's human question. Staging events are not shown as user questions.

Existing subscriptions are left in place (including their cursors and user-chosen modes).
Missing active-card/child subscriptions are re-registered; a historical gap is reported because
new subscriptions start at the latest event. Previous rapid results remain in Notion, but are
not claimed as channel-delivered. Native notifier retention/failure removal can remove a
subscription. Gateway routing and connected channel availability remain necessary; registration
alone does not prove delivery. Native retries, cursors, ping/wake checkpoints and clients are
untouched. There is no exactly-once delivery promise.

Primary channel outlets: cron deliver for semantic run summaries/source incidents; native
Kanban notify for work status/results. Notion holds persistent questions/results. Prompts
avoid repeating Kanban outcomes in cron summaries or proactive send_message. Native children
inherit routes through creator/decomposition and can also inherit via dependency parents;
this may produce one notification per child. Notification inheritance is not proof of ownership.

## Installation and export

Known project files are backed up before replacement; no generic three-way merge is active.
Unknown files/jobs are preserved and reported. Backups include old AGENTS, live cron definitions,
skill, baselines/conflicts, rule archive, database snapshot and install record. Database schema 9
adds child associations, target-card binding, pinned routes and predecessor delivery linkage.
Schema migration first takes an online SQLite backup; data is not deleted.

Program upgrade and personal preference import are separate. The installer archives old
material without classifying it. An explicitly requested Hermes conversation can review the
archive into native USER.md; rejected, ambiguous or oversized content remains archived.
Uninstall removes recorded owned cron resources and marked generated files, preserves modified
skills and never deletes Task/Notion data, profile memory or Kanban history.

Exports contain Task/Project/evidence/history, business associations and state; framework
version, templates, effective managed files, live cron config/prompt and old customization
archives. The manifest explicitly excludes native memory/identity, full Kanban history,
notification delivery checkpoints and gateway sessions, and names separate backup locations.
Importing the business workspace does not write profile memory or re-install native cron jobs.

## Reliability follow-up to 8e177d3

See [six-finding review evidence](reliability-review-2026-10-06.md). Schema 10 persists unfinished
child discovery and child attention. Root completion does not erase a failed discovery retry;
only a complete pass clears that responsibility. Individual child read/notification failures
cannot discard siblings' successful business synchronization. Completed history is not polled
forever. Notification repair is independently recorded in health and respects restore hold,
pause and cancellation before releasing a staging block.

Answers bind exact delivery/card/event/version identities for roots and children. Child answers
do not increment the entry's requirement version. Actionable child questions/failures contribute
to the existing blocked presentation label without changing the native root status. Notion view
version 3 includes actionable labels in focus, subject to task status, next actor and snooze.
Closed human tasks keep their status and have no active Agent work presentation label.
