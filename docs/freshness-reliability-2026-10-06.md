# F01–F05: input freshness and recoverable incremental reads

Start: `cbd2af23a31a248eea42ed9ff22a2ee05e47844c`, also fetched `origin/main`.
The working tree was clean. This is a reliability patch, not a deployment or architecture change.
Hermes still owns the two cron jobs, context injection, prompts/skills/memory, workers,
Kanban scheduling and notification transport. Todo Manager owns the data and recovery below.

## Findings and fixes

| Finding | Reproduction and cause | Repair | Behaviour evidence in `tests/test_freshness_audit.py` |
|---|---|---|---|
| F01 | A complete pending creation feedback kept an obsolete body. Updating its payload alone would still authorize an agent holding the old interpretation. | `notion/sync.check_body` creates a successor feedback identity for changed body **or completeness**, supersedes the pending predecessor even when claimed, and preserves both. `ops.page_body_unread` applies the existing covering-read rule to `new_task` too. `perform` rechecks persisted status before mutation; already successful identical operations still replay their op-log result. | `BodyFreshness.test_creation_changed_body_invalidates_claimed_context_and_retries`, `test_creation_read_failure_and_same_minute_edit_do_not_allow_mutation`; `BodyCrossChecks.test_incomplete_then_full_then_retraction_rejects_all_old_contexts` |
| F02 | The managed-region fingerprint could stay equal while the API changed from truncated to complete; the early return lost that change, and automatic retries only considered incomplete creation feedback. | `body_unread/check_bodies` include pending `body_edit` incompleteness; `_box_read` versions changes in completeness as well as content. Partial reads cannot become a covering read. Full recovery supersedes the old incomplete interpretation. | `BodyFreshness.test_complete_reread_recovers_identical_region_automatically`; `BodyCrossChecks.test_truncation_inside_box_then_changed_full_read_and_claim` |
| F03 | The first replacement inherited Retry-After but the next replacement ignored the delayed pending effect. | `outbox.enqueue` includes pending deadlines in the same target/op/entity inheritance. `effects._push` still deduplicates identical projections and preserves uncertainty/exhaustion; its task projection now also includes unresolved remote project intent. | `ProjectionRecovery.test_three_replacements_inherit_deadline_for_tasks_and_projects`; `ProjectionCrossChecks.test_uncertain_blocks_new_projections_without_delaying_other_entity`; the F03/F04 cross-chain below |
| F04 | An explicit clear of a locally NULL project looked like a no-op, leaving a remote unresolved relation to resurrect later. | `store.update_task` treats an accepted cancellation of pending intent as a versioned, audited change. It checks human protection before removing intent, even for equal column values. `defer_notion_project` versions the remote choice when first observed, so a stale agent cannot bypass protection using override. CLI, direct requests and Notion properties converge on the same store path. | `PendingRelation.test_explicit_clear_cancels_unresolved_relation_and_survives_restart`; `RelationCrossChecks` covers rejected agents, stale override, repeated clears, Notion property clears and rate-limit/restart recovery |
| F05 | Every history increment searched all pages of the configured mailbox query before intersecting candidate threads. | `GmailSource.fetch` restricts server query evaluation to the RFC Message-IDs in each changed thread, in chunks of ten; it then hydrates the selected thread. History, selection and window pagination, hydration progress and text-part caches are checkpointed in the existing cursor. The gate passes its remaining deadline and atomically persists successful progress while retaining an actual error/failure count. | `IncrementalQuery` measures 100-page mailbox costs; `GmailBudgetChecks` covers partial pagination, nonmatches, complex query, expiry, old parent/new reply, request/time budgets, error/restart, legacy queues and multipart progress |

All five were reproduced before implementation. The initial six regression methods produced
seven assertion failures (Task/Project are separate subcases). A later clean `git archive`
replay of the seven principal methods against the original baseline produced **nine assertion
failures, zero execution errors**. That replay also covers matching at the end of a large
mailbox and a completely nonmatching candidate. No existing reliability test was deleted.

Three existing R tests changed assumptions, while retaining their guarantees:

- `test_b_unread_body_recovers_before_delegation_without_duplicates`: follow the successor
  feedback, wait for a covering read, still require exactly one task/delegation and replay.
- `test_b_empty_body_is_complete_but_api_truncation_retries`: the old incomplete snapshot stays
  in history as superseded; its complete successor is actionable.
- `test_r15_query_selection_is_paginated_and_backlog_hydrates_full_thread`: selection now
  continues per candidate across ticks, rather than completing a whole mailbox search first.
  Full reply context, excluded candidates and no repeated search of a selected thread remain asserted.

## Cross-chain results

- **F01/F02:** incomplete creation body → complete successor → later retraction, including a
  same-minute edit → both previously captured contexts rejected before delegation → covering
  reread → new context updates successfully → restart and identical retry return the stored
  result. After resolution, later notes do not create another creation request.
- **F03/F04:** unresolved remote project → push limited for one hour → explicit clear → two
  further summary changes and repeated dirty scans → restart → Project import recovers → no
  early send and no revived relation → due push contains the cleared project and latest summary.
- **F05:** selection/history/window pagination crosses multiple ticks, retaining the old
  history watermark until completion. No selected material means `wakeAgent:false`. Once a
  candidate matches, the adapter hydrates eligible thread context; normal maturity policy still
  decides when model work is due. Persistent errors retain failure counts and health state.
- Existing R01–R16 regressions and the full suite retain hold, question/approval identity,
  cancellation ownership, subscription recovery, workspace ownership and unknown-action protections.

## Gmail API semantics and measured cost

Official documentation read for this patch:

1. [messages.list](https://developers.google.com/gmail/api/reference/rest/v1/users.messages/list):
   `q` uses Gmail search syntax, explicitly including `rfc822msgid`; pagination is via pageToken.
2. [Search/filter differences](https://developers.google.com/workspace/gmail/api/guides/filtering):
   the API does not perform Gmail UI thread-wide search; Unix seconds can express precise dates.
3. [Search operators](https://support.google.com/mail/answer/7190?hl=en): `rfc822msgid`, OR,
   grouping and conjunction. Todo Manager does not parse the configured query locally.

For an incremental candidate, all messages' Message-ID headers are read with
`threads.get(format=metadata, metadataHeaders=Message-ID)`. Queries have the form
`(<configured query>) (rfc822msgid:<id> OR ...)`. A returned message must belong to the candidate
thread; duplicated Message-IDs outside that thread do not count as a match. All candidate
header chunks must finish before declaring a nonmatch. A match ends selection early. This
retains the previous meaning: any matching message in a changed thread selects the thread,
then full eligible context is read, including nonmatching replies and old parents.

Measured with a synthetic 100-page mailbox and one changed thread with ordinary Message-IDs:

| Scenario | Baseline search calls | Fixed search calls | Fixed total Gmail calls |
|---|---:|---:|---:|
| Match on mailbox page 1 | 100 | 1 | 4: history + metadata + selection + full thread |
| Match on mailbox page 100 | 100 | 1 | 4 |
| No matching candidate | 100 | 1 | 3: history + metadata + selection |

Daily cost normally scales with changed threads and their message count: one metadata read,
up to `ceil(unique Message-IDs / 10)` query groups per candidate, plus full reads for selected
threads and separate text-body parts when needed. A restricted query can still paginate
(e.g. duplicate RFC identifiers); those pages are checkpointed too. Search is still performed
by Gmail; these are API request-count guarantees, not a promise about Google's internal work.

Each adapter call starts at most **40 Gmail API requests**, and checks a **180-second** local
budget plus the gate's remaining budget before each request. HTTP timeout is capped by the
remaining budget. The token helper has its existing separate subprocess timeout, and HTTP
read timeouts are not a strict whole-process kill timer. Every completed page is checkpointed;
uncompleted selection is not a negative result. The original watermark is retained until the
scan and hydration queue finish. A checkpointed failure persists partial progress and reports
failure; it is not a successful idle read.

**Boundaries:** missing, multiple or unusually encoded Message-ID headers cannot safely be
converted to search terms. Those candidates fall back to the original configured server query,
with early termination on a match and the same per-tick request/time bounds. A complete
nonmatch in that fallback may still cost the entire matching mailbox across many ticks.
The 100-page missing-header test explicitly needs all 100 pages, never more than four calls
per test tick, and eventually returns the candidate. Extremely large threads, duplicated RFC
identifiers and initial/expired-history window backfills can also cost more. No arbitrary
Gmail internal thread-ID search operator or approximate local query evaluator is used.

Initial backfill and expired-history fallback retain their configured time window/overlap.
Normal history selection has no new time restriction, so an old matching parent with a new
reply is not lost. Mailbox/API changes during pagination are not an atomic snapshot; existing
overlap, next-history reads and stable source identities remain the recovery boundary.
Live Gmail query execution was not tested in this round; API contracts and synthetic transport
assertions are the evidence. No Gmail credentials or real content were accessed.

## Compatibility, operation and rollback

- Schema remains **10**. No new tables, CLI commands or config keys; no dependency changes.
- Pending feedback IDs may now be superseded by a full or newer read. Read the successor
  indicated in `feedback item` and interpret it again; do not resubmit a new operation under
  an obsolete identity. An already committed identical operation remains replayable.
- Pending project intent cancellation can now report `project_id` as applied and increment
  Task version even when the SQL column was already NULL. History includes the remote intent.
- Gmail cursors contain an additive `scan` checkpoint. Old `history_id/queue/queue_history_id`
  cursors are accepted. Text-body parts can temporarily be held in the checkpoint while a
  multipart message finishes; treat database/export files as private source data.
- A source failure does not consume the unresolved page or new history watermark. Leave the
  cursor intact and let intake continue. No additional cron, worker or model call is needed.
- Native prompt/skill/memory ownership and runtime settings are unchanged.

Before a future authorized upgrade, back up the chosen workspace using the existing CLI.
This command is documentation, not a production action performed by this patch:

```bash
TODO_HOME=/path/to/chosen/workspace python3 -m todo_manager backup
TODO_HOME=/path/to/chosen/workspace python3 -m todo_manager export --verify
```

For an isolated development checkout, update normally and run the verification commands below.
A production installation still requires the existing native job pause/upgrade procedure;
this repair round did not perform it.

Rollback warning: replacing a database/workspace can discard changes made after its backup.
Preserve the current database/export first. Schema 10 is readable by the baseline, but the
baseline **does not understand new Gmail scan checkpoints and reintroduces F01–F05**. Prefer a
forward fix. If rollback is necessary, stop the owning processes, preserve current data, and
use the matching pre-upgrade backup through the existing `restore` preview/confirmation flow
in an isolated rollback workspace. Never copy Hermes jobs.json or profile memory. Same-schema
logic changes do not create an automatic pre-schema backup; make the explicit backup above.

## Reproducible verification

```bash
# Focused synthetic behaviours (23 methods).
python3 -B -m unittest -v tests.test_freshness_audit

# Full standard-library/Fake suite; native tests deliberately excluded here.
TM_HERMES_PYTHON=/nonexistent python3 -B -m unittest discover -s tests -t .

# Real installed Hermes code, synthetic workers, isolated HERMES_HOME/boards/workspaces.
HERMES_DISABLE_LAZY_INSTALLS=1 \
TM_HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python \
python3 -B -m unittest discover -s tests/integration -t .

# Reproduce the five findings and API call costs against the audit baseline without
# changing this checkout. The expected outcome is 7 tests, 9 assertion failures.
TM_FRESH_BASELINE=$(mktemp -d /tmp/tm-freshness-baseline.XXXXXX)
git archive cbd2af23a31a248eea42ed9ff22a2ee05e47844c | tar -x -C "$TM_FRESH_BASELINE"
cp tests/test_freshness_audit.py "$TM_FRESH_BASELINE/tests/"
(cd "$TM_FRESH_BASELINE" && python3 -B -m unittest -v \
  tests.test_freshness_audit.BodyFreshness \
  tests.test_freshness_audit.PendingRelation \
  tests.test_freshness_audit.ProjectionRecovery \
  tests.test_freshness_audit.IncrementalQuery)
```

Native source rechecked: Hermes **0.21.5+4108**, commit
`105568c1558e1cb494e6edf1a2998d524ebe809e`, existing authorized venv. No native interface or
state transition was changed by this patch. No real model, Notion or channel test was run.

## Recorded final results

| Layer | Passed | Failed | Skipped | Not run |
|---|---:|---:|---:|---|
| Full discovery with native interpreter deliberately unavailable | 631 | 0 | 24 native tests, run separately below | None of the standard-library/Fake suite |
| Installed Hermes real-code integration suite | 24 | 0 | 0 | Real-model execution and real channel transport |
| Focused F01–F05 synthetic behaviours (subset of full suite) | 23 | 0 | 0 | Live Gmail/Notion API execution |

Full discovery: 655 tests in 63.839 seconds. Independent native suite: 24 tests in
253.707 seconds. Focused suite: 23 tests in 2.804 seconds. These counts do not treat skipped
native tests as passed, and the focused count is not added to the full-suite count.
