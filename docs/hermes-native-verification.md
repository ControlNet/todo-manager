# Hermes-native verification and rollout

## Baseline and native evidence

Checked 2026-10-06: Todo Manager HEAD and remote main initially both
`a820251aa95376e7d728d3c8c8eb13b86217956a`; working tree initially clean. Installed Hermes
source is `~/.hermes/hermes-agent`, commit
`105568c1558e1cb494e6edf1a2998d524ebe809e`, install stamp base version **0.21.5**,
display version **0.21.5+4108.g105568c**. No Hermes source patch is part of this change.

Official documents were read from that exact checkout under `website/docs/`: features/cron,
features/context-files, features/memory, features/kanban, features/kanban-worker-lanes and
related implementation files. These are official repository documents, not assumptions about
unreleased behavior. The links below pin the source revision.

| Native capability | Source in Hermes at the verified revision | Connected Todo behavior |
|---|---|---|
| Stored prompt, skills, script, workdir, deliver, failure_deliver | [`cron/job_definition.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/cron/job_definition.py), `hermes_cli/cron.py` | Installer uses native cron create/edit/remove. No runtime prompt_file interface or direct store writes |
| Wake gate, material/skill assembly, Script Error | [`cron/scheduler_prompt.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/cron/scheduler_prompt.py): `_parse_wake_gate`, `_build_job_prompt`, `_load_cron_skill_parts`; `cron/scheduler.py` around 2217/2435 | Direct bounded script context, native skills, no business writes on Script Error, explicit idle JSON |
| Project priority and SOUL | [`agent/prompt_builder.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/agent/prompt_builder.py): `discover_context_files`, `build_context_files_prompt`, `_load_soul_md` | workdir discovery; health checks actual selected type/files, including ancestor files and override priority |
| Profile memory and approval | [`tools/memory_tool.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/tools/memory_tool.py), `memory_tool_store.py` | USER at profile `memories/USER.md`; no preferences loader; enable flags and frozen snapshot verified |
| Card creation, worker context, blocking, completion/archive | [`hermes_cli/kanban_db.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/hermes_cli/kanban_db.py): `create_task`, `build_worker_context`, state functions | Thin Python adapter; business IDs/versions, reconciliation and root protocol remain local |
| Claims, profiles, workspaces, review | [`hermes_cli/kanban_db_dispatch.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/hermes_cli/kanban_db_dispatch.py): `dispatch_once`, `_worker_argv`, `_default_spawn`, `_profile_exists_fn` | Native claim/execution; dispatchability checked; no worker scheduler added |
| Decomposition and ownership | [`hermes_cli/kanban_db_graph.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/hermes_cli/kanban_db_graph.py), `kanban_decompose.py`, `kanban_specify.py` | Creation-event lineage query, child question/result transfer; no dependency executor |
| Native notification registration/cursor | [`hermes_cli/kanban_db_notify.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/hermes_cli/kanban_db_notify.py): `add_notify_sub`, `list_notify_subs`, `unseen_events_for_sub` | Explicit pinned route, blocked staging then registration/unblock, missing-subscription repair |
| Delivery, failures, wake checkpoint and retention | [`gateway/kanban_watchers_notifier.py`](https://github.com/NousResearch/hermes-agent/blob/105568c1558e1cb494e6edf1a2998d524ebe809e/gateway/kanban_watchers_notifier.py), notification checkpoint helpers | Gateway owns transport/retries/checkpoints; adapter never writes cursor rows |

Internal Python APIs are version-dependent: the bridge deliberately imports native functions
rather than modifying native state tables. Its direct SQL is read-only legacy identity/event
lookup (including archived cards). `_profile_exists_fn`, `discover_context_files` and memory
flag resolution are thin native inspection dependencies, not replacement implementations.
An incompatible version must be tested and reported before deployment.

## Test tiers and actual scenarios

All fake input is synthetic test data. There are no production Notion writes, source writes,
channel messages, GitHub comments or model calls in this round.

1. **Fake/standard-library tests:** existing lost create/comment responses, restarted retries,
   manual field protection, versioned review/blocked answers, cancellation/approval interleavings,
   unknown external actions, outbox recovery and migrations remain covered. Updated ownership
   assertions verify backup/replacement rather than the retired three-way-merge contract.
   New tests cover subscription staging failure/recovery, no route, child question identity,
   stale child replies, ownership-only cancellation, partial results, schema-8 backup and
   done successor linkage.
2. **Real Hermes code with simulated worker, no model:** `test_hermes_kanban` retains its legacy
   isolated manual-review fixture to test historical reliability interleavings. The new
   `test_native_contract` uses default orchestration settings, without disabling decomposition
   or review. It calls native dispatch with a simulated spawn callback, native decomposition,
   context builders, memory store, notification APIs and real cron CLI commands.
3. **Real model:** not run. The feedback decision in the end-to-end test is explicit synthetic
   test input using production Todo operations. The native decomposer persistence path is
   exercised; its auxiliary model's ability to copy constraints is not claimed.

The native contract probe checks these separately:

- Native blocked staging prevents dispatch until the native subscription exists; an actual
  dispatch callback receives the claimed card, valid profile and isolated scratch directory.
- A subsequent native blocked event is visible to the existing subscription. Re-registering
  an existing route does not reset the cursor. Late subscriptions have no replay, as documented.
- Native decomposition proves creator lineage and inherits passive routes. A dependency-only
  outsider is excluded from ownership. Archiving the root does not archive the child and can
  make the outsider ready.
- Long card material is retained, while worker context is clipped. The minimum protocol and
  full-text command survive at the front. Decomposer input is shorter. Workers do not receive
  the Todo skill unless configured on their card.
- AGENTS, AGENTS.override, HERMES priority, independent SOUL, and unchanged memory snapshots
  during a session are exercised by real native loaders. Native skill + script stdout and
  Script Error assembly, explicit idle gating and empty successful script output are checked.
- Synthetic Notion next_actor=Agent creates one request. Retried feedback decision creates
  one delegation. A simulated lost real-card-create response is recovered without a second
  card. The native dispatcher executes the simulated worker. A child question reaches the
  original Notion Task; a reply to its discussion returns to that child, once, while the root
  remains blocked.
- Real native cron CLI creates two paused jobs, edits their runtime settings and prompt, and
  verifies upgrade replaces only the managed prompt while preserving settings and pause state.
  The old prompt is in the backup. Uninstall removes these owned jobs and retains workspace data.

Run independently from the repository root:

```bash
TM_HERMES_PYTHON=/nonexistent python3 -B -m unittest discover -s tests
python3 -B -m unittest tests.integration.test_native_contract
python3 -B -m unittest tests.integration.test_hermes_kanban tests.integration.test_hermes_prompts
python3 -B -m unittest tests.test_native_links tests.test_native_migration tests.test_hermes_install
```

Expected: `OK`; the first command intentionally skips native tests. Each native test uses a
temporary HERMES_HOME and temporary workspace. Existing Hermes venv use was explicitly
approved by the user. No dependency installation is needed. `AGENTS.md` and `pyrightconfig.json`
record that environment only.

### Recorded results, 2026-10-06

- Standard-library/Fake suite: 561 discovered, 538 executed successfully, 23 native tests
  intentionally skipped (`TM_HERMES_PYTHON=/nonexistent`), 58.623 seconds.
- Final native contract: 1 subprocess test containing the eight scenarios listed above,
  passed in 24.942 seconds after the final prompt update.
- Real Hermes reliability/context suites: 22 passed in 211.835 seconds, including lost
  responses, cancellation/approval ordering and native context contracts.
- Focused final checks: installation 29 passed, framework assets 2 passed, configuration 3 passed.
- No real model test or channel transport test was run.
- Staged `secret-guard` scan passed. One historical synthetic GitHub token fixture initially
  matched the scanner and was changed to an explicit short fake value; no real credential
  was involved.

### Native CLI bootstrap finding

The first isolated native-CLI attempt timed out because the installed launcher's
`venv_sync.prepare_launch` tried source-update dependency completion for a new home. It also
left a temporary Web build directory in the Hermes source checkout. The child processes were
stopped; that newly created directory was moved to `/tmp/tm-aborted-hermes-web-build-ivqsHC`
for inspection and the Hermes git worktree was verified clean. The installed release stamp
was unchanged. The attempted PM generation was under the temporary test home, not the existing
venv. This failed attempt is not counted as a passing test.

The installer now explicitly sets **HERMES_DISABLE_LAZY_INSTALLS=1** for its own native CLI
subprocess, an official `prepare_launch` guard. It also pins HERMES_HOME and scrubs inherited
profile routing variables. The successful rerun uses the real CLI with that guard, not a fake
cron store. No global Hermes setting or source file was changed to bypass the guard.

## Development installation

The following creates only disposable integration assets; it does not configure sources,
Notion, a gateway, worker profiles or a delivery channel. Both jobs stay paused.

```bash
TASK_SANDBOX=$(mktemp -d /tmp/todo-native-install-XXXXXX)
export TODO_HOME="$TASK_SANDBOX/workspace"
export HERMES_HOME="$TASK_SANDBOX/hermes"
export HERMES_DISABLE_LAZY_INSTALLS=1
./bin/todo-manager init --language en
./bin/todo-manager config set hermes.home "$HERMES_HOME"
./bin/todo-manager config set hermes.launcher "$TASK_SANDBOX/bin/todo-manager"
./bin/todo-manager hermes install --dry-run
./bin/todo-manager hermes install --yes
./bin/todo-manager hermes status
./bin/todo-manager export --verify
./bin/todo-manager uninstall --dry-run
```

Expected: two paused jobs, local delivery, direct gate scripts, managed skill and AGENTS. Source
health may report that sources have never run; this is not a failed framework installation.
Do not resume these disposable jobs. Uninstall `--yes` removes only owned installed resources;
business data and board history remain. No production installation was performed in this round.

## Migration and rollback

Database migration to schema 9 is automatic on first open, with a pre-schema online backup.
Framework installation separately captures `backups/framework/upgrade-*`. Verify those
backups before replacing old managed files. Old AGENTS, prompts, skill, baselines/conflicts,
rules archive and install record are retained. Personal-preference import remains explicitly
pending; ask Hermes once to review the archive under the native memory limits/approval policy.

The standard-library migration test verifies the version-8 backup remains readable and that
version-9 integrity/foreign-key checks pass. For a copy of an existing development workspace,
a rollback must preserve the post-upgrade database before restoring the earlier snapshot.
The next commands deliberately move the active development database; stop its Todo processes
and keep both files. They assume the development TODO_HOME above contains a pre-schema-v9 backup.

```bash
./bin/todo-manager hermes pause --yes
python3 - <<'PY'
import os, sqlite3
from pathlib import Path
home = Path(os.environ['TODO_HOME'])
backups = sorted((home / 'backups').glob('pre-schema-v9-from-v8-*.sqlite'))
if not backups:
    raise SystemExit('No schema-8 backup here; nothing changed')
path = home / 'state/todo.sqlite'
if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm')):
    raise SystemExit('Close all database connections and checkpoint first; nothing changed')
saved = home / 'backups/post-v9-preserved.sqlite'
if saved.exists():
    raise SystemExit('Preserved database already exists; choose another destination')
path.rename(saved)
source = sqlite3.connect(f'file:{backups[-1]}?mode=ro', uri=True)
target = sqlite3.connect(path)
source.backup(target)
target.close()
source.close()
print('Schema-8 database restored; use baseline a820251 code in a separate checkout')
PY
```

Run old code from a separate checkout of `a820251`; do not run schema-9 commands against that
restored database unless intending to migrate again. Restore the matching owned framework
files from the framework backup. For cron rollback, feed each saved `cron.json` prompt to
native `hermes cron edit --prompt` with structured subprocess arguments; preserve the user's
runtime settings. Never replace the native jobs store directly. USER/MEMORY/SOUL are not part
of this restore.

## Remaining boundaries

No transport delivery was sent: registration and unseen-event behavior are verified, actual
Matrix/Slack/email delivery depends on a configured gateway/profile client. Business processing,
Notion synchronization and channel delivery are distinct. Late re-subscription does not recover
missed native alerts. There is no exactly-once promise or automatic intelligent child alert
aggregation.

Whole-delegation atomic pause and child-specific one-use external approvals are not implemented.
Pause explicitly reports root-only scope; child approval is refused. Cross-child requirement
redistribution/integration remains the native root agent's responsibility, not an always-on
Todo coordinator. Automatic decomposer constraint inheritance is not guaranteed; remote workers
without access to material references must ask for input. See the architecture for precise
cancellation ownership, late results, review and done behavior.

If an entry delegation has already ended while a descendant remains blocked, its late question
is retained for inspection, but answers through that closed delegation are rejected. Resolve
the orphaned native card explicitly or create a new linked delegation; this round does not
automatically reopen the entry or transfer its authority. Child result event metadata and
attachment references are retained; native child artifact bytes are not independently archived
by Todo Manager. The entry worker is responsible for incorporating them into the final delivery.
