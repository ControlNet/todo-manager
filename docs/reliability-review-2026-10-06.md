# Reliability review fixes (2026-10-06)

Starting HEAD and origin/main were both `8e177d3aebff84137c9ce6d3f287e94f821a4bb9`;
the working tree was clean. All six findings still reproduced. Installed Hermes remains
`105568c1558e1cb494e6edf1a2998d524ebe809e` (0.21.5+4108.g105568c).
This round changes cross-system recovery only: two crons, native dispatch, native notifications,
and existing profile/context loading remain in place.

## Findings and regression evidence

Six initial tests in `tests/test_review_reliability.py` failed before implementation:

| Finding | Observed failure before fix | Fix and regression coverage |
|---|---|---|
| Hold bypass through notification repair | Staged card became ready after restore, before pending resume was sent | Shared release predicate checks hold, pause and pending cancel. Root sync and relinking use it. Read-only synchronization continues. Tests include pause/resume/restore/restart/repeated sync/release, cancellation, and real native dispatcher exclusion |
| Old root question answered a new block | Explicit old delivery plus approval was accepted | Resolve the exact delivery for explicit, Notion discussion and implicit-current entry points. Bind card/event/requirement version; reject stale/cross-delegation targets before recording approvals. Operation identity includes the question, including explicit keys. Root and child tests cover normal replies, duplicate requests and lost comment/unblock responses |
| Ended root lost child discovery retry | Next sync checked zero delegations after failed first discovery | Persist `children_sync_pending` with root progress, clear only after complete child enumeration/read. Per-child read failures do not stop siblings. Restart, partial success and newly discovered children are tested; settled history stops being scanned |
| Notification fault hid results | Root stayed queued with no delivery after remote completion | Repair errors use per-card notification health. Root/child snapshots, event persistence and Notion effects proceed independently. Relinking an uncertain completed create also survives notification failure. Tests assert no duplicate business comments or effects after recovery |
| Missing execution profile never recovered | Fake accepted an empty profile; real bridge rejects it | Fake now validates like the bridge. Install/health report not ready for missing/invalid profiles. Retry first looks up remote identity, then repairs an invalid unsubmitted profile from valid configuration, records the change and creates once. Existing/uncertain accepted cards are linked without reassignment |
| Child question hidden behind queued root | Agent label remained queued and actionable views omitted the task | Persist child attention as question/failure independently of native status. Aggregate actionable children into existing blocked label. Focus and review views use the same label as local export. Dependency/transient waits and resolved history are excluded; human next_actor/status protection remains |

Additional pre-fix failures also reproduced a hold arriving during profile preflight and a
remaining question in a parallel delegation being hidden after another child was answered.
Both now have passing regressions. Additional regressions cover pending child answers not changing the inferred root requirement
version; a recovered old child answer not clearing a newer question; stale replies returning
explicit errors; live pending/sent/superseded/error status on answer command retries; and ended
root questions remaining visible without reopening the root or granting new authority.

Old tests were retained. Adjustments are limited to:

- Shared synthetic test workspace explicitly configures the valid fake `default` profile,
  rather than relying on FakeKanban accepting an empty assignee.
- Schema-8 upgrade assertions now expect the current schema and current backup filename.
- Existing Notion view-upgrade assertions include the newly managed focus filter; their
  custom-view preservation and removed-view checks remain.

## Native contracts checked

Source at the pinned Hermes revision:

- `hermes_cli/kanban_db_dispatch.py::_profile_exists_fn` checks actual profiles and the
  dispatch profile allowlist. `_resolve_default_assignee` and `_apply_default_assignee`
  implement a native configured default for unassigned cards. Todo continues requiring an
  explicit valid `kanban.assignee`, avoiding implicit global reassignment. No profile router
  or global config changes were added.
- `kanban_db.py::create_task`, `unblock_task`, `block_task`, `complete_task` and the native
  dispatcher are used by the isolated probe. A triage card cannot be completed by assuming
  it has the same API behavior as a blocked card; the test uses native respecification first.
- `VALID_BLOCK_KINDS` is dependency, needs_input, capability, transient; NULL is the native
  generic human blocker. `gave_up` identifies terminal retry-budget failure. Child presentation
  uses these values and persisted native events, not inferred task graphs.
- `kanban_db_notify.py::add_notify_sub` starts a new subscription at the current latest event.
  Registration repair does not rewind cursors. The real-code probe verifies an existing
  subscription cursor remains unchanged while business synchronization recovers.

`tests/integration/review_contract_probe.py` runs the production bridge entrypoint in-process
inside a subprocess with a temporary HERMES_HOME. Only its transport is in-process; database,
profile, subscription, state and dispatch operations are real installed Hermes code. Worker
spawn callbacks and all task/Notion inputs are explicitly synthetic. No gateway notifier,
real model or actual channel client is invoked. Existing integration suites retain their
separate subprocess transport tests.

## Operational behavior and compatibility

Schema **9 -> 10** adds only:

- `delegations.children_sync_pending`, initially 1. Existing ended delegations receive one
  recovery discovery pass; settled historical delegations are then excluded again.
- `delegation_children.attention`, NULL / question / failure. The native status column remains
  native state. This is a presentation/recovery record, not an execution state machine.

An online `pre-schema-v10-from-v9-*.sqlite` backup is created before migration. Tests verify
old rows/cursors, foreign keys, integrity, repeated migration and restoration of the readable
schema-9 backup. No data or old approvals are deleted. Export includes the added columns.

No new configuration key or CLI subcommand is required. `delegate show` now includes child
status/attention and the durable pending-discovery flag. `delegate answer` returns the exact
question delivery ID and current message status; `pending` means queued, not delivered.
Explicit `--key` is scoped to that question, so another question cannot return a misleading
cached answer result. Child-specific external approvals remain unsupported.

For an already configured **development** workspace, set the existing execution profile and
let the normal feedback gate reconcile pending submissions:

```bash
./bin/todo-manager config set kanban.assignee default
./bin/todo-manager health
./bin/todo-manager gate feedback
./bin/todo-manager delegate list
```

Use `default` only if that native profile is valid and allowed for dispatch in the selected
Hermes home. A missing or invalid profile remains local and is reported. Profile repair happens
only after remote lookup successfully finds no card; an uncertain lookup never permits creation
or reassignment. A repair is recorded in delegation version and a durable task comment effect.
Already associated work is never reassigned by a configuration edit.

After restoring a development database, inspect the board and pending messages before explicitly
releasing its existing hold:

```bash
./bin/todo-manager health
./bin/todo-manager delegate list
./bin/todo-manager delegate release-hold
./bin/todo-manager gate feedback
```

Releasing hold resumes the existing pipeline; it does not create a new scheduler. A hold is not
an emergency stop for workers already running in Hermes. It prevents Todo-triggered submission,
message delivery and release of initial staging blocks. It cannot atomically retract a remote
operation already in flight when the hold is established.

Notion view definitions move to version 3. The existing installer offers to update the owned
focus filter only when it still matches its earlier definition. Custom filters are retained and
reported. No production Notion update was performed during this work. Use the existing install
flow on the intended environment to review and apply those view changes.

For rollback, stop the development processes first. **Restoring the database replaces active
state**: preserve the schema-10 database and its WAL consistently using SQLite backup, then
restore the pre-schema-10 backup and run the earlier code from a separate checkout of
`8e177d3`. Do not open the restored database with newer code unless intending to migrate again.
The migration regression performs this restore into a separate file and checks schema-9 data.
This does not roll back native Kanban actions, Notion writes or profile assets; use the existing
restore hold and reconciliation before releasing work. Never copy native jobs/state tables.

## Verification commands

```bash
TM_HERMES_PYTHON=/nonexistent python3 -B -m unittest discover -s tests
python3 -B -m unittest tests.integration.test_review_contract
python3 -B -m unittest discover -s tests/integration
python3 -B -m unittest tests.test_review_reliability tests.test_native_migration tests.test_notion tests.test_install
git diff --check
```

The first command deliberately skips installed-Hermes tests. Native tests use the user-approved
existing venv, temporary boards/workspaces/home, and no model calls. Native CLI tests disable
lazy install. No dependencies were installed or modified. Exact final results are recorded
below after the final checks.

## Boundaries

An ended entry is never reopened merely to recover child data. Its unresolved child question is
visible, but answering through the closed delegation is rejected; inspect the native child or
create a separately authorized linked follow-up. A success in Notion/outbox synchronization is
not a channel-delivery receipt. New late subscriptions cannot replay old notifications. Existing
native transport retry/cursors remain Hermes-owned. No live model, production Notion, actual
channel transport, global Hermes settings or production cron were exercised in this round.

## Recorded results

- Initial six-case red run: 6 failures, one for each finding, before implementation.
- Additional red runs: hold during preflight and cross-delegation child attention each failed
  before their fixes. These scenarios are included in the final regression suite.
- Final standard-library/Fake run: 594 discovered, **570 passed**, 24 installed-Hermes tests
  deliberately skipped, 60.415 seconds. This includes 29 targeted review regressions and a
  synthetic Notion round trip checking the actual Agent work property after each child answer.
- Full real-Hermes integration run: **24 passed**, 240.949 seconds.
- After the last execution-guard/attention changes, both native contract probes were rerun:
  **2 passed**, 29.041 seconds (8 original scenarios plus 4 review scenario groups).
- Real model, live Notion and actual channel delivery tests: not run by task scope.
- Python syntax, diff whitespace and staged secret scans passed. Localization checks: 11 passed.
