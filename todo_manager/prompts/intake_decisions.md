## Intake decisions format (this text comes from the installed todo-manager version and is authoritative)

Write one JSON object and pass it to `todo-manager intake apply <run_id> --file <path>`:

    {"run_id": "<run id>", "rules_version": <int>, "items": [ <op>, ... ]}

Every NEW source item listed above must be covered exactly by one of: evidence of an op, or an `ignore` op.
Human-facing text (titles, next_step, responsibility, notes) is in {language}; keep names, code,
repository and paper titles as written in the source.

Ops:
- `create_task` {ref?, title, responsibility, evidence: [{item, role, note?}], project?, status?, next_actor?,
  next_step?, done_criteria?, summary?, due_date?, due_basis?, follow_up_date?, waiting_on?, focus?, focus_reason?,
  follow_up_of?, ask?, rationale}
  - `responsibility`: one sentence on why this is the user's own commitment (asked directly, promised,
    owner/reviewer/assignee, deadline addressed to them). No responsibility, no task.
  - `project`: an existing project id or `@ref` of a `create_project` in this file.
  - `status`: todo | in_progress | waiting | decision | review; `next_actor`: me | agent | external
    (waiting implies external; decision/review imply me).
  - Dates are `YYYY-MM-DD` in the user's timezone. Set `due_date` only when a deadline is stated or clearly
    implied, and say where it came from in `due_basis`. `follow_up_date` is when to check on someone else.
  - `focus` only for genuinely important or time-critical work; say why in `focus_reason`.
  - `follow_up_of`: the task id this continues (use it instead of reopening a task the user closed).
  - `ask`: one question for the user, only for an important ambiguity (see below); requires
    `status: "decision"` and `next_actor: "me"`. At most {max_questions} per run.
- `update_task` {task, based_on_version, set: {field: value}, note?, evidence?, override_human?, closed_reason?, rationale}
  - `based_on_version` is the version shown for the task. Fields the user set later are kept automatically.
  - Changing a field the user set requires `override_human: true` plus evidence; prefer not to.
  - `note` is a short "what changed" line shown on the home page (what changed and why).
  - Closing: `set: {"status": "done"}` only on clear evidence that the commitment is fulfilled
    (reply sent, PR merged, meeting held); commits are not completion.
- `link` {task, evidence, rationale}: new messages that belong to an existing task without changing it.
- `ignore` {items: [ids], category, rationale}; category is one of advertising, newsletter, cfp,
  review_invitation_unaccepted, automated_notice, fyi_no_action, handled_elsewhere, social, spam, other.
- `create_project` {ref, title, domain?, goal?, next_step?, repos?, evidence?, rationale}: only for a sustained
  stream of work the user owns; prefer an existing project.
- `update_project` {project, based_on_version?, set: {goal|next_step|blockers|recent_activity|status|domain}, repos_add?,
  evidence?, rationale}. Commit digest: summarise per project in `recent_activity`; never one task per commit.
- `suggest_rule` {title, body, scope_type?, scope_id?}: a generalisation for the user to confirm (not binding).
- `followup_reviewed` {task, rationale}: a due follow-up was looked at and needs no change today. Every
  follow-up listed for this run must be covered: `update_task`/`link` on that task, a `create_task` with
  `follow_up_of`, or `followup_reviewed`.

One task per commitment, not per message. A thread that adds information to an existing task is `link` or
`update_task`, never a duplicate.
When unsure whether something is the user's: if it is low-value or the evidence points nowhere in particular
(mass mail, FYI, someone else's thread, a vague mention), `ignore` it with a rationale. Ask only when missing
it could cost the user (a direct request from a real person, a stated deadline, money/grants, reviews, hiring,
a role assigned to them) and the evidence cannot settle who owns it or what is wanted: then `create_task` with
`ask` (the one question the user answers to settle it) and say in `rationale` what is unclear. Questions are
few by design, not a review queue for everything uncertain.
On error the command prints `{"error": CODE, "details": [...]}` and applies nothing: fix the file and re-run.
`STALE_RULES` means rules changed: re-read them (`todo-manager rule list`) and re-check your decisions.
A repeated apply of the same file (e.g. after a lost response) prints the first report with `"duplicate": true`;
`ALREADY_APPLIED` means this run was applied with a different file: stop, nothing more to do. `STALE_RUN` means
this run's work was taken over by a newer run: stop.
