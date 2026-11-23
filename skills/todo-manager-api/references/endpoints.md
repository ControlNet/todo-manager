# Todo Manager API: every operation

`tm` is the curl function from SKILL.md. `{id}` is a task (`tsk_...`), project (`prj_...`),
delegation (`dlg_...`), delivery (`dlv_...`) or source item (`src_...`) id. The heading names the MCP
tool that does each operation, with the same fields as arguments: `show` reads any id, `search`
also searches the material, `message_delegation` also answers and approves (`kind`).

## Reading

### overview: `GET /api/home`
Tasks grouped by what each needs (`decide`, `mine`, `agent_wait`, `agent_run`, `waiting`), counts, projects.

### list_tasks: `GET /api/tasks?project=prj_xxx&closed=true`
Task cards: `id, title, kind, status, next_actor, project, next_step, waiting_on, due, follow_up,
snooze_until, focus, ...`. Without `closed`, open tasks only.

### show: `GET /api/tasks/{id}`
The card plus `summary, responsibility, done_criteria, due_basis, sources` (with their words),
`thread` (comments, replies, deliveries), `agent_work, agent_question, notes, links, history`.

### overview: `GET /api/projects` · show: `GET /api/projects/{id}`

### search: `GET /api/search?q=words`
`{tasks, projects, sources}` containing the words.

### show: `GET /api/deliveries/{id}`
A delivered version: its notes (`body`) and `version.files`; a file is
`GET /api/deliveries/{id}/files/{n}`.

### list_delegations: `GET /api/delegations` · show: `GET /api/delegations/{id}`

### search: `GET /api/sources?q=words&mine=true&since=2026-11-01&limit=20`
Stored mail, Slack and GitHub items with an excerpt each; `mine=true`: what the user wrote.

### show: `GET /api/sources/{id}`
One item with its full text (`body`).

### status: `GET /api/status?details=true`
Task counts, waiting material, agent work, last runs, `problems`.

## Changing tasks

### act_on_task: `POST /api/tasks/{id}/act`
```json
{"action": "done"}
{"action": "wait", "params": {"on": "Alice", "follow_up": "2026-12-05"}}
{"action": "due", "params": {"date": "2026-12-01"}}
{"action": "snooze", "params": {"until": "2026-12-03"}}
{"action": "project", "params": {"project": "prj_xxx"}}
```
Actions: `done, todo, cancel, reopen, agent, wait, snooze, due, follow_up, focus, project, pause, resume`.
A date of `null` clears it.

### comment_on_task: `POST /api/tasks/{id}/comment`
```json
{"text": "Shorter, please", "to": "agent"}
```
`to`: `hermes` (default; the feedback run reads it) or `agent` (the agent working on the task).

### create_task: `POST /api/tasks`
```json
{"title": "Book the venue", "project": "prj_xxx",
 "set": {"due_date": "2026-12-01", "next_step": "Ask Alice for dates"},
 "evidence": ["src_xxx"], "key": "venue-2026-11-23"}
```

### update_task: `PATCH /api/tasks/{id}`
```json
{"set": {"summary": "...", "next_step": "...", "status": "waiting", "waiting_on": "Bob",
         "follow_up_date": "2026-12-05"},
 "note": "Bob asked for a week"}
```
Fields: `title, project_id, status, next_actor, waiting_on, responsibility, next_step,
done_criteria, summary, due_date, due_basis, follow_up_date, snooze_until, focus, focus_reason,
decision`. Statuses: `todo, in_progress, waiting, decision, review, done, cancelled`. Next actor:
`me, agent, external`.

## Projects

### create_project: `POST /api/projects`
```json
{"title": "Thesis", "set": {"goal": "Submit by March"}, "repos": ["owner/repo"]}
```

### update_project: `PATCH /api/projects/{id}`
```json
{"set": {"next_step": "..."}, "repos_add": ["owner/other"]}
```
Fields: `title, domain, status, goal, next_step, blockers, icon`.

## Handing over to the feedback run

### record_feedback: `POST /api/feedback`
```json
{"said": "Move everything for the workshop to next week", "scope": "global"}
```
`scope`: `task:<id>`, `project:<id>` or `global`. The next feedback run interprets it as it would
a Notion comment.

## Agent work

### create_delegation: `POST /api/delegations`
```json
{"task": "tsk_xxx", "brief": "Draft a reply to Bob: ... Deliver the draft as Markdown.",
 "allow": ["read", "analyze", "draft"], "start_after": "2026-11-24 09:00"}
```

### message_delegation: `POST /api/delegations/{id}/messages`
```json
{"kind": "revision", "body": "Shorter, and mention the deadline"}
```
Kinds: `supplement, revision, pause, resume, cancel, accept`.

### message_delegation, `kind` answer: `POST /api/delegations/{id}/answer`
```json
{"body": "Yes, send it to Alice only", "approve": ["send_email:to Alice only"]}
```

### message_delegation, `kind` approve: `POST /api/delegations/{id}/approve`
```json
{"delivery": "dlv_xxx", "action": "send_email", "detail": "to Alice only"}
```

## Errors

`401 {"error": "LOGIN"}`: token missing or replaced. Otherwise `{"error": "<CODE>", "details": [...]}`
with 400 (invalid), 404 (not found), 409 (a request id reused for other words) or 503 (busy: retry
later).
