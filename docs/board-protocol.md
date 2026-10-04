# Hermes Kanban board protocol

Explicit agent work ("delegations") runs as cards on ONE dedicated Hermes Kanban board,
`todo-manager` (config `kanban.board`). There is no extra manager layer: the Todo Manager
feedback gate submits cards, relays your answers and reads results deterministically; the
Hermes dispatcher runs the worker; the model is only woken for your feedback.

## Required Hermes configuration

| Setting | Required | Why |
|---|---|---|
| `kanban.review_dispatch: false` | yes | Deliveries wait in `review` for **you**. With the default `true`, the dispatcher spawns an `sdlc-review` worker on every review card (any board), which may complete the card on its own. This switch is global in Hermes. |
| `kanban.auto_decompose: false` | yes | The loop breaker can park a card in `triage`; auto-decompose would fan it out into new cards via the auxiliary model. Also global. |
| `kanban.auto_subscribe_on_create` | no change | Cards are created through the bridge without a chat session, so nothing is subscribed. |
| board `todo-manager` | create once | `hermes kanban boards create todo-manager --name "Todo Manager" --description "Delegations from Todo Manager"` |

todo-manager reads both switches (read-only, bridge `settings` op, resolved like the installed
dispatcher: unset means `true`) before it creates a card and on every feedback-gate run while cards
are in flight. If either is on, **no new card is created** (delegations stay `pending_submit`) and
`todo-manager health` shows the exact `hermes config set ...` command; cards already on the board
are not cancelled but are exposed (an sdlc-review worker can complete or send back a review card).
todo-manager never changes the Hermes config itself. Check them with:

```bash
hermes config get kanban.review_dispatch
hermes config get kanban.auto_decompose
```

`kanban.assignee` (empty = board default) picks the Hermes profile that executes delegations;
`kanban.max_running` (default 1) limits how many delegations execute at once — further ones
wait locally as `queued` and are submitted when capacity frees up.

## Lifecycle

```
pending_submit ──submit (lock, lookup, create)──▶ queued ──▶ running
      │                                              ▲          │
      └──▶ reconcile (create response lost) ─────────┘          ├─ kanban_block(needs_input) ─▶ blocked ─ your answer ─▶ running
                                                                ├─ kanban_request_review ─────▶ review ─┬─ revision ─▶ running
                                                                │                                        └─ accept ─▶ ended
                                                                ├─ gave up (failure limit) ───▶ blocked (decide retry/cancel)
                                                                └─ pause ─▶ paused ─ resume ─▶ running
any state ── cancel ──▶ cancelled (card archived)
multiple matching cards ──▶ quarantined (reported, nothing dispatched)
```

Card status → delegation status: ready/todo/scheduled → `queued`; running → `running`;
blocked/triage → `blocked` (or `paused` when we paused it); review → `review`; done → `ended`;
archived → `cancelled`.

The Task follows: an executing delegation sets the task to In progress / next by Agent; a
question or delivery sets it to To review / next by Me; ending or cancelling returns it to To do /
Me unless other agent work is active. **A finished card never closes the Task**: whether the
task is done is decided from its done criteria and later evidence (or by you).

## Card content

The card body starts with machine-readable lines, then the brief, then the worker protocol:

```
todo-manager-delegation: <delegation id>
todo-manager-task: <task id>  notion: <task page url>
requirement-version: <n>

## Goal / Context / Constraints / Deliverable / Done when / Task background / Later supplements
## Todo Manager delegation protocol   (English; names the user's language for questions and summaries)
```

The protocol tells the worker to do only this delegation, never touch Notion or task status,
ask with `kanban_block(kind="needs_input", ...)`, deliver with
`kanban_request_review(summary, metadata.deliverable_markdown, artifacts)` (never
`kanban_complete`), follow comments by `todo-manager`, and stop immediately on a comment that
starts with `[todo-manager pause]`.

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
| answer | comment + unblock — only for the question it answers (`target_block_seq`); an old reply can never unblock a newer question; respecify when the loop breaker parked the card in triage |
| revision | comment + reopen review |
| approval | comment `APPROVED <action> for delivery <id>`; bound to that delivery version — a newer delivery revokes it |
| pause / resume | comment + block with our marker / unblock of our own pause only |
| cancel | archive (terminates a running worker) |
| accept | complete the review card |

Every comment carries `[tm:<message id>]`, so a retry after a lost response does not comment
twice. External actions (email, public comments, push, PRs, closing issues, publishing) are
allowed only when listed in the delegation's allowed actions or approved by you for the exact
delivered version; approvals and delegations requested by anyone other than you are refused.
These rules reach the worker as card text; the worker's tools are not intercepted (see
limitations). Cards are created with `max_retries = 1`, so a failed run (crash, timeout, stale
claim) parks the card for you instead of a respawn that could repeat an approved action.

## Results

Every gate run reads new card events: questions and failures become a delivery record plus a
task comment (and an optional Matrix reminder); `review` results become a numbered delivery
(`v1`, `v2`, …) shown in the task page's managed box, with artifacts listed. Workspaces of
finished cards are deleted by Hermes, so workers keep needed files as artifacts.
