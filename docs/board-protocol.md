# Hermes Kanban board protocol

Explicit agent work ("delegations") runs as cards on ONE dedicated Hermes Kanban board,
`todo-manager` (config `kanban.board`). There is no extra manager layer: the Todo Manager
feedback gate submits cards, relays your answers and reads results deterministically; the
Hermes dispatcher runs the worker; the model is only woken for your feedback.

## Hermes configuration

Hermes owns execution: worker start and claim, concurrency, queue, retries and recovery. Todo
Manager neither sets nor requires Hermes settings; `install` only creates the board (after your
confirmation) and enables the Kanban integration.

| Setting | What it means for this board |
|---|---|
| board `todo-manager` | created once by `todo-manager install` (`hermes kanban boards create todo-manager ...`) |
| `kanban.review_dispatch` (Hermes default `true`) | when on, Hermes' `sdlc-review` worker takes every card in `review` on every board and may complete it or send it back before you look. Global in Hermes. |
| `kanban.auto_decompose` (Hermes default `true`) | when on, Hermes fans `triage` cards out into new cards on every board; the loop breaker parks a second same-kind question in triage. Global in Hermes. |
| dispatcher limits (`kanban.max_in_progress`, ...) | how many cards run at once, on every board |

`todo-manager health` shows what the two switches mean while they are on (read once a day,
as notes, not problems), with the command to turn them off if you want deliveries to wait for
you:

```bash
hermes config get kanban.review_dispatch
hermes config get kanban.auto_decompose
hermes config get kanban.auto_decompose          # read only; this integration does not disable orchestration
```

`kanban.assignee` (empty = board default) picks the Hermes profile that executes delegations.
The earlier local capacity (`kanban.max_running`) is gone; `health` notes it while config.toml
still sets it.

## Lifecycle

```
pending_submit ──submit (lock, lookup, create)──▶ queued ──▶ running
      │                                              ▲          │
      └──▶ reconcile (create response lost) ─────────┘          ├─ kanban_block(needs_input) ─▶ blocked ─ your answer ─▶ running
                                                                ├─ kanban_request_review ─────▶ review ─┬─ revision ─▶ running
                                                                │                                        └─ accept ─▶ ended
                                                                ├─ kanban_complete ───────────▶ ended (delivered)
                                                                ├─ gave up (failure limit) ───▶ blocked (decide retry/cancel)
                                                                └─ pause ─▶ paused ─ resume ─▶ running
any state ── cancel ──▶ root archived + proven descendants archived (termination reported)
multiple matching cards ──▶ quarantined (reported, nothing dispatched)
```

Card status → delegation status: ready/todo/scheduled → `queued`; running → `running`;
blocked/triage → `blocked` (or `paused` when we paused it); review → `review`; done → `ended`;
archived → `cancelled`.

The Task's status is yours: card events never change it. The delegation's own state is the
Task's "Agent work" column (queued, running, needs my answer, to review, delivered, paused,
problem). One default links them: delegating makes the agent the Task's next actor; while the
work waits for you (a question, a delivery, a failure, or it ended or was cancelled) the next
actor goes back to you, and to the agent again when the work resumes, unless you set the next
actor yourself after delegating. **A finished card never closes the Task**: an agent's draft is
delivered, and whether the task is done is decided by you (or later evidence). Closing the Task
(done or cancelled) cancels its active agent work; it never accepts a delivery (an accept you
gave explicitly before closing is still carried out).

## Card content

The card body starts with machine-readable lines, then the brief, then the worker protocol:

```
todo-manager-delegation: <delegation id>
todo-manager-task: <task id>  notion: <task page url>
requirement-version: <n>

## Goal / Context / Constraints / Deliverable / Done when / Task background
## The user's notes on the task page (context)   (when the page has notes outside the box)
## Later supplements
## Todo Manager delegation protocol   (English; names the user's language for questions and summaries)
```

The protocol tells the worker to do only this delegation and report on this card, leave the
Notion task and its status alone, and use Hermes' native tools: `kanban_block(kind="needs_input")`
for a question and for approval of an external action it is not allowed yet,
`kanban_complete(summary, metadata.deliverable_markdown, artifacts)` when done, or
`kanban_request_review` for native execution review; human questions use blocked. Nothing
forces review. It follows comments by `todo-manager` and stops immediately on a comment that
starts with `[todo-manager pause]`. Cards created by earlier versions keep the protocol in their
body (deliver for review, no `kanban_complete`) and are synced the same way.

## Identity and duplicates

- The delegation id is assigned once; the Kanban idempotency key is derived from it, and the
  body carries the id as a marker line.
- Submission (lookup → create → record card id) runs under the `kanban-submit` file lock that
  every entry point uses. Before creating, cards are looked up by key and marker, including
  archived ones. A failed or uncertain lookup never leads to a create.
- The state is set to `reconcile` before the create call; a crash or a lost response is
  repaired by the next lookup, never by a second create. Several matching cards quarantine
  the delegation and raise an alert.

## Messages to a card

Your instructions are stored first (`delegation_messages`) and then delivered by the gate:

| Kind | Delivery |
|---|---|
| supplement | comment (a running worker receives it as a steer) |
| answer | comment + unblock — only for the question it answers (its `blocked` event); an old reply can never unblock a newer question; respecify when the loop breaker parked the card in triage. `delegate answer --approve <action>[:<detail>]` adds one line `APPROVED <action> for question #<n>. <detail>` per approval to that comment (built when it is posted, so a voided approval is never in it); the approvals count as handed when the unblock lands and are void, with a notice, when the answer cannot reach its question |
| revision | comment + reopen review — of the version it was written for (bound when queued); a newer recorded version makes it unused and you are told. Unbound (nothing delivered yet): the card is checked when it is sent — a version already in review is reopened even if our last sync saw the worker running; otherwise the comment reaches the running worker as a steer |
| approval | (`delegate approve --delivery`) comment `APPROVED <action> for delivery <id>`; bound to that delivery version — a newer delivery revokes it. Each approval is its own message; all approvals for one version are commented first and the review is reopened once. One that can no longer reach that run (the version was sent back, or approvals for it are in flight) is refused when received. The run's outcome (`reported` with the next version, `unknown` + pause when it ended without one) is taken from the first result or run end after that reopen, also when the reopen is confirmed only after the sync read that end |
| pause / resume | comment + block with our marker / unblock of our own pause only. A pause does not stop the lookup of a card whose create response was lost: a found card is linked and paused at once. "Paused" means the newest block on the card is ours; a pause requested while the worker waits for an answer shows as "blocked, pause requested" |
| cancel | archive (terminates a running worker). Processed before any other message; messages that have not taken effect are void (approvals revoked) and you get one notice naming them; one already in flight is checked on the card and kept as history if it landed. While a cancel is pending, answer/revision/resume/accept/approval are refused. After the archive, every approval already handed to a run is settled from all of the card's events: `reported` (that run delivered), `unknown` (it ended without a result, or was still going or waiting for an answer when archived; you are told, nothing is paused or resumed) or `not_run` (no run claimed the card after the approval) |
| accept | complete the review card — only while the version you accepted is the one in review (bound when queued). Refused when nothing delivered is in review; a retry checks its bound review event first, so a card completed meanwhile counts as sent. An accept that completed the card before a cancel ran ends the delegation as done (the cancel is unused) |

Every comment carries `[tm:<message id>]`, so a retry after a lost response does not comment
twice. Each comment's own event and the card event before it are recorded; a delivery's
requirement version counts only the messages whose comment event precedes its result event
(the card body is position 0), so a steer that reached the worker before it submitted counts for
that result and one posted after it does not. A transition (unblock, reopen review, complete) is checked against its bound event and
applied in one bridge process, but not in one transaction: Hermes' `write_txn` refuses nesting,
and `complete_task` & co. must not run under an outer transaction. When a dashboard action lands
between the check and the call, the card's events show a newer question/version before ours;
the bridge reports `raced`, the message is kept as sent and marked, and you are told. External actions (email, public comments, push, PRs, closing issues, publishing) are
allowed only when listed in the delegation's allowed actions or approved by you for the exact
question or delivered version, once; approvals and delegations requested by anyone other than
you, or by a scheduled run, are refused. Every approval is recorded with who gave it and what
became of it (`reported`, `unknown`, `not_run`). An approved action whose run ended without a
report is neither treated as not done nor repeated: the card is paused and you are asked.
These rules reach the worker as card text; the worker's tools are not intercepted (see
limitations). Cards allowed an external action are created with Hermes' `max_retries = 1`, so a
failed run (crash, timeout, stale claim) parks the card for you instead of a respawn that could
repeat it; other cards use `kanban.max_retries` (default 1; 0 = Hermes' own default).

## Results

Every gate run reads new card events: questions and failures become a delivery record plus a
task comment; `review` and `completed` results become a numbered delivery
(`v1`, `v2`, …) shown in the task page's managed box, with artifacts listed. A card the worker
completed ends the delegation ("delivered" in the Agent work column until you close the Task or
delegate again). Workspaces of
finished cards are deleted by Hermes, so workers keep needed files as artifacts.


## Native children and current scope

See [architecture](architecture.md#children-questions-completion-and-cancellation) for schema-9
child bridging. Creator/decomposition events establish ownership; dependency links do not.
Child questions are linked to exact deliveries and cards; Notion discussion replies route back
to them. Direct replies can name `delegate answer --question <delivery-id>`. Child results stay
partial. Child-specific external approval is refused: do the explicitly authorized external
operation on the root through the existing version-bound protocol.

Pause/resume, supplement and revision currently affect the entry card. A pause with descendants
adds a scope warning in Notion. Cancellation archives the root first and observes/archives
only proven descendants; a native archive is not recursive and may release unrelated dependants.
Unconfirmed termination is reported. No board-wide or dependency-graph cancellation occurs.
Done-card changes create a successor delegation on the same Task, with prior delivery linkage.
Notification registration and channel delivery are independent from Notion sync state.
