# Concentrated reliability audit: R01–R16

Audit and starting checkout: `6b02612b5b19c11384664586ceb500f1e49983e8`.
The working tree was clean before this round. `origin/main` was fetched and still matched
that commit. This is a reliability patch, not another architecture migration.

All external-service fixtures below are **synthetic test doubles**. No production workspace,
Notion page, cron, Hermes setting, model call or channel client was used. The existing Hermes
venv was used without installing or updating dependencies.

## Implementation and regression map

Every numbered finding was reproduced on the audit baseline. The initial red tests were
followed by a replay of the corrected, expanded tests in a temporary `git archive` checkout.
Tests live in `tests/test_concentrated_audit.py`; names below omit the `test_` prefix.

| Finding | Root cause and shared fix | Behaviour regression |
|---|---|---|
| R01 | Global requirement version was being used as an open-question identity. `delegation.add_message`, `child_links.queue_answer/deliver_answer` now check the specific card/event and open native state. Ordinary answers survive supplements and other cards' answers. External approvals still require the current requirement version; supplements still void unsent grants. | `r01_supplement_does_not_close_the_same_question`, `r01_root_answer_and_supplement_keep_child_question_open`, `r01_changed_requirements_do_not_reuse_question_authorization`; real Hermes probe |
| R02 | Explicit selection accepted only question deliveries. `ops.delegate_answer` accepts question/failure consistently. Child `gave_up` now creates a persistent failure delivery and replies use that precise native failure event. | `r02_explicit_failure_reply_can_retry`, `r02_child_failure_has_exact_recoverable_target` (lost unblock, restart, later failure) |
| R03 | Terminal business selection discarded pending subscription repair. `sync_cards` includes durable notification failures for roots and children. After 12 consecutive registration failures a terminal card has an explicit exhausted/manual-repair conclusion; no endless historical business scan. A recovered subscription clears the registration fault and persists a `history_gap` observation separately. | `r03_terminal_notification_registration_retries`, `g_last_child_notification_repair_survives_terminal_sync`; real Hermes cursor probe |
| R04 | Duplicate source identities discarded changed Slack conversation linkage. `_insert_item` promotes linkage in place for all stored versions without replacing item/batch/evidence identity. `_read_thread` retains a returned parent as context even on repeat reads. | `r04_parent_keeps_identity_when_promoted_to_thread`, `r04_slack_old_parent_promotion_preserves_processed_batch_and_evidence` |
| R05 | A failed body read was stored as a complete empty body; completeness was checked only after mutations. New-task feedback records actual read completeness and retries incomplete reads. A successful covering read refreshes pending feedback. `ops.perform` checks fresh feedback validity inside the mutation transaction, before calling the operation. Read and clarification paths remain available. | `r05a_unread_new_task_is_not_complete_input`, `r05b_incomplete_feedback_cannot_commit_mutation`, both `b_` chains |
| R06 | `_sync_body` omitted `_box_read`'s truncation argument and used stale editor attribution. It now passes actual truncation and freshly retrieved editor identity. Truncated reads never overwrite a managed region. | `r06_managed_box_race_preserves_human_text`: verifies text retention and human feedback attribution |
| R07 | The latest bot acknowledgment displaced the original delivery anchor. Discussion lookup is page-scoped, prefers the original persistent `dlv:` identity, and rejects ambiguous/missing identities rather than falling back to the latest question. Recording a bot response can enrich an already polled comment's ref. | `r07_confirmation_keeps_discussion_delivery_anchor`, cross-chain `a_` for root and child, with late external approval |
| R08 | Root status alone defined active business ownership. Shared `unfinished_sql` includes pending child discovery, live owned children and unconfirmed stopping; used by duplicate creation guards and Task lifecycle cancellation. Ended/archived roots can be cancelled without reopening. Failed discovery/archive and lost responses retain the pending cancel. | `r08_cancel_ended_root_stops_owned_running_child`, cross-chain `c_`; real Hermes owned-vs-dependency cancellation probe |
| R09 | Every dirty scan generated a new effect and superseded failed work. `effects._push` fingerprints the desired local projection (including delegation display, evidence and deliveries), reuses unfinished/dead same-projection work, and preserves attempts/uncertainty. Changed projections inherit outstanding retry deadlines; uncertain older pushes reconcile before newer pushes. A pending body fingerprint distinguishes a lost program write from a human edit even after a later local change. | `r09_dirty_scan_preserves_retry_after`, `r09_project_permanent_error_is_not_reset_by_dirty_scan`, cross-chain `d_`, existing `UnconfirmedWriteTests` |
| R10 | Project relinking lacked Task's duplicate-page guard. The project import retains the original link, reports and isolates the copy, and continues other pages. | `r10_duplicate_project_does_not_abort_pull`, cross-chain `e_` |
| R11 | Unknown project pages and explicit empty relations both became `None`. An unresolved remote target is retained in existing metadata, included in the outbound projection and retried after pulls/dirty scans. A later explicit local project choice cancels that pending relation. | `r11_unknown_project_relation_survives_push`, `r11_later_local_project_choice_wins_over_unresolved_relation`, cross-chain `e_` |
| R12 | Matching comment text was incorrectly treated as delivery proof. Each new comment carries a readable `TM effect <digest>` identity. Recovery matches bot, page, effective discussion and identity. Page fallback is persisted before sending. Invisible or legacy unmarked uncertain sends remain uncertain, without automatic replay. | `r12_equal_text_in_other_discussion_is_not_delivery_proof`, `r12_same_discussion_equal_text_distinct_effects_and_lost_response`, `r12_fallback_target_is_durable_when_response_is_lost` |
| R13 | Repo relations bypassed project version/provenance/history. Repos now participate in the same accepted/rejected field update and transaction as other project fields; creation records their provenance too. | `r13_repo_edits_are_versioned_and_human_protected`, `r13_notion_repo_edit_has_history_and_blocks_stale_agent` |
| R14 | Foreign generated scripts conflicted but jobs and skill were still adopted. A common ownership conflict check guards planning, apply, individual job/skill actions and uninstall. Trusted own manifest IDs allow user-changed workdir. Conflicting workspaces acquire no ownership. | `r14_other_workspace_never_adopts_or_removes_owned_resources`, existing installer upgrade/partial/foreign-ID tests |
| R15 | Gmail history has no search-query parameter; changed threads bypassed selection. History candidates now intersect a paginated server-side `messages.list(q=...)` result before full-thread hydration. No local Gmail-query parser. Persisted selected backlog is hydrated across ticks. | `r15_history_honors_server_query`, `r15_query_selection_is_paginated_and_backlog_hydrates_full_thread`, existing initial/expired-history tests |
| R16 | Skipped or incomplete repo reads were treated as an empty completed day. Digest readiness requires all repo reads to complete. Each completed repo records the day in its existing cursor; later ticks read remaining scopes in least-recently-attempted order. No model attempt is consumed by partial reads. | `r16_partial_digest_does_not_consume_daily_completion`, cross-chain `h_` with real gate/persistence and synthetic adapters |

## Cross-chain evidence

| Chain | Verified outcome |
|---|---|
| A | Real Notion sync code with FakeNotion: Q1 → reply → bot acknowledgment → Q2 → old discussion amendment keeps Q1's identity. Root and child variants reject late answers/approvals; a supplement alone does not expire Q1. |
| B | Repeated 503 body reads prevent delegation; a full later read restores the same Task/feedback and a repeated delegation command stays idempotent. Empty body and API truncation have different outcomes. |
| C | Root done → Task cancelled → child discovery fails → restart → archive response lost → restart → confirmed owned-child stop. Unrelated cards remain running. Native probe also tests a real dependency edge without ownership. |
| D | Lost Notion write → repeated dirty scans → a new human projection → reconciliation and convergence. Retry-After survives scans, permanent failures are not reset, and uncertain body echoes do not become human edits. |
| E | Project pull 503 while Task pull succeeds → relation preserved through push → restart → projects recover. A duplicated project is isolated while the unknown target resolves; explicit relation clearing still works. |
| F | A installs → B conflicts on the same profile/launcher → B applies/uninstalls its plan → A's cron JSON and skill remain intact. Existing setting-preservation/upgrade tests still run. |
| G | Last root/child result and subscription failure occur together → restart → business effects remain unique → subscription repair finishes. Native cursor begins at the current event; no historical replay is claimed. |
| H | A synthetic first repo consumes the read budget → no daily completion/model attempt → restart → only the unread second repo is fetched → empty day completes with zero model attempts. |

## Native/API basis

Installed Hermes source was rechecked at `105568c1558e1cb494e6edf1a2998d524ebe809e`
(native `get_version_info()` returned `0.21.5+4108`). The integration interpreter is
`~/.hermes/hermes-agent/venv/bin/python`.

- `hermes_cli/kanban_db_notify.py:add_notify_sub`: idempotent subscription identity is
  task/platform/chat/thread; a new cursor starts at `MAX(task_events.id)`. Re-registering an
  existing subscription preserves its cursor. `remove_notify_sub` is used only on synthetic
  subscriptions in the probe.
- `hermes_cli/kanban_db.py:unblock_task`: blocked/scheduled to resumable phase, re-gated on
  parents. `specify_triage_task` handles triage; the existing thin bridge checks exact events.
- `hermes_cli/kanban_db.py:archive_task`: archives one task and attempts termination of that
  task's host-local worker. It is not recursive cancellation. `create_task(parents=...)`
  creates dependency edges; `creator_task_id` and decomposition creation events identify
  ownership independently.
- `hermes_cli/kanban_db_dispatch.py:dispatch_once`: remains the only execution dispatcher.
  The probe passes a simulated spawn function; it does not call a model.
- [Gmail history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)
  has history/label filters but no `q`.
  [Gmail messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
  documents server-side `q` and pagination. Matching messages select threads; hydration then
  keeps eligible messages from those threads, including replies that do not match `q`.
- [Notion list comments](https://developers.notion.com/reference/list-comments) returns
  unresolved comments, with `discussion_id` and rich text. Absence from that list does not prove
  an uncertain write did not happen. This is why recovery does not blindly resend.

The running pipeline and responsibility split in [architecture.md](architecture.md) are
unchanged: two native cron scripts prepare/synchronize data; Hermes supplies context,
agent execution, Kanban scheduling and channel delivery. Todo Manager retains cross-system
identity, transactions and recovery. No scheduler, worker loop, notification sender,
preference rule system, prompt loader or additional cron was added.

## Recorded results

| Layer | Passed | Failed | Skipped | Not run |
|---|---:|---:|---:|---|
| Full standard-library/Fake discovery | 608 | 0 | 24 native tests, intentionally excluded | None in this layer |
| Separate actual Hermes integration discovery | 24 | 0 | 0 | None in the available native suite |
| Concentrated regressions (subset of the first row) | 38 | 0 | 0 | None |
| Real model / production Notion / real channel transport | 0 | 0 | Not counted as automated skips | Entire layer, intentionally not run |

After the full native run, both modified/relevant native contract probes were re-run and
passed (two test methods, containing 15 scenario groups in total). This does not add to the
24 unique native tests above. Final standard-library discovery is 632 discovered tests:
608 pass plus 24 explicit environment-disabled native skips.

The final baseline replay contains 38 methods: 37 expose old failures and one pre-existing
protection passes. Unittest reports 29 assertion failures and 9 error events because the
root/child variants of chain A are separate subtest error events. All direct R01–R16
reproductions fail on the baseline and pass in the updated checkout.

## Compatibility and operational boundaries

- **Schema stays at 10.** No SQL migration is needed. New recovery data uses existing
  `meta`, cursor metadata and outbox JSON payloads; SQLite backup/export includes it.
- **No new config keys or CLI commands.** `--question` now consistently accepts current
  failure deliveries. Existing users still configure one execution profile and one optional
  explicit notification route.
- New comments include a short visible effect identity. Old uncertain comments without it
  require manual reconciliation; an invisible marked comment also stays uncertain. No
  exactly-once guarantee is made. Independent discussions are not serialized behind such an
  uncertain comment; ordering barriers apply only to projection pushes.
- Same-projection failed/dead effects keep their original deadline/attempts. A genuine new
  projection can supersede unsent work but does not bypass a server's outstanding deadline.
  Handlers continue to read current protected local state when the effect becomes due.
- A root that has ended can still own unfinished work and block default new delegation.
  Explicit `delegate create --parallel` remains the exception for intentionally separate work.
  Root completion does not grant child external authority. Residual ended-delegation questions
  are retained and clearly marked as residual work: cancel the delegation before a follow-up;
  ordinary replies cannot reopen an ended delegation. Child-specific external approvals remain
  unsupported and explicitly refused. An active delegation's child failure is answerable.
- Registration recovery is not channel delivery. `health` exposes registration failures and
  the durable `history_gap` observation separately. Hermes owns sending, cursors and retries.
  A terminal registration that exhausts 12 attempts has a manual-repair conclusion; no business
  result is lost and no historical event is claimed to have been sent.
- Gmail's query may require a paginated mailbox search on a history tick with candidates.
  This trades additional reads for exact Gmail query semantics. The adapter's existing
  eligibility exclusions (spam, trash, drafts, chat) remain; query configuration narrows this
  supported scope. A queued backlog uses the query selection made when it was listed.
- Slack promotion preserves batch membership. Older thread coverage still obeys configured
  parent scan horizon/page budget; this patch does not promise discovery outside that horizon.
- Managed-region edits are protected by observed reads and native API checks, not a Notion
  compare-and-swap primitive. Hermes transitions also retain the existing post-transition race
  detection; no stronger atomicity is claimed.

## Upgrade, backup and rollback

This round did not run an installer or deploy against a production workspace. Normal upgrades
use the existing installer and ownership checks. Before upgrading an actual configured
workspace, set `TM_WORKSPACE` to its path, pause its two owned cron jobs through Hermes, then:

```bash
: "${TM_WORKSPACE:?Set TM_WORKSPACE to the intended workspace path}"
python3 -B -m todo_manager --home "$TM_WORKSPACE" backup
python3 -B -m todo_manager --home "$TM_WORKSPACE" export --verify
```

Keep the printed backup/export paths. Update the checkout with ordinary Git fast-forward
workflow and run the existing install preview/application procedure in
[operations.md](operations.md). This patch itself needs no database migration and does not
change user cron settings, memory or global Hermes options.

**Rollback replaces data if you restore a backup.** Prefer first validating rollback in an
isolated directory, retaining the current backup/export. Use a separate detached worktree
at the old commit, not `reset` or force push. Old code lacks these fixes and must not run
against unresolved new recovery work. An imported/restored workspace enters delegation hold;
review links and unknown external actions before explicitly releasing that hold. Never
restore an entire Hermes profile's USER/MEMORY/SOUL for a Todo database rollback.

## Reproducible verification

From the repository root, Fake/standard-library regression (the native skips are intentional):

```bash
TM_HERMES_PYTHON=/nonexistent python3 -B -m unittest discover -s tests -t .
python3 -B -m unittest -v tests.test_concentrated_audit
```

Actual installed Hermes code, temporary homes/boards/workspaces and simulated workers:

```bash
HERMES_DISABLE_LAZY_INSTALLS=1 \
TM_HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python \
python3 -B -m unittest discover -s tests/integration -t .
```

No real-model test was run. No channel delivery or production API end-to-end test was run.
The native probe's seven scenario groups include the original hold/old-question,
child-response-loss, discovery/notification and profile tests plus this round's open-question,
terminal-subscription and ended-root-cancellation paths.

To replay the new behavioural regressions against the audit baseline without changing your checkout:

```bash
TM_BASELINE=$(mktemp -d /tmp/tm-audit-baseline.XXXXXX)
git archive 6b02612b5b19c11384664586ceb500f1e49983e8 | tar -x -C "$TM_BASELINE"
cp tests/test_concentrated_audit.py "$TM_BASELINE/tests/"
(cd "$TM_BASELINE" && TM_HERMES_PYTHON=/nonexistent python3 -B -m unittest -v tests.test_concentrated_audit)
```

Expected: the baseline run fails with concrete assertion/operation errors for all R01–R16;
the current checkout passes. Exceptions such as the R06 missing argument and R10 unique-link
constraint are the reproduced defects, not skipped tests.

Existing tests were retained. Four fixture/expectation updates are intentional:
Gmail body fixtures now return a server query selection; two comment assertions allow the
readable identity suffix; the response-loss correction test waits for the preserved retry
window; the schema-9 migration fixture writes the real old row shape rather than invoking
new schema-10 business code against an intentionally old database.
