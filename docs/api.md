# API for agents

Agents on other machines (Claude Code, Codex, Cursor, Hermes, OpenClaw, ...) read and change Todo
Manager over HTTP, or connect to it as an MCP server. Both are served by the [Web UI](web-ui.md)'s
server, so the Web UI must be on, and reachable from where the agent runs (see *Turning it on*
there).

The API is another way in, not another backend: its reads are the Web page's and the command
line's, its writes go through the same operations as `todo-manager task update` and the page's
buttons.

## Signing in

Agents sign in with what signs in to the Web UI, its token or your password, as a Bearer token:

```bash
todo-manager web token          # print the token
todo-manager web token --new    # replace it: browsers and agents sign in again
todo-manager web password       # set or change the password (also signs them out)
```

```
Authorization: Bearer <token or password>
```

A request with this header needs nothing else (no cookie, no `X-TM-Request`). A password that
matched is remembered until it changes, so only an agent's first call checks it the slow way.

## What a write is

A write through the API is your own edit, as if you made it on the page or with `--human`: it
wins over later changes by the intake agent, and its history shows `origin` `api`. Agents should
make the changes you asked for.

Optional `key` (a string you choose) names a write: sending it again with the same key applies it
once. Without a key, every call applies.

## Operations

Reads take URL parameters; writes take a JSON body. The MCP tool of each is named in the first
column.

| Tool | Method and path | What it does |
|---|---|---|
| `overview` | `GET /api/home` | Tasks grouped by what each needs, counts, projects |
| `list_tasks` | `GET /api/tasks?project=&closed=` | Open tasks as cards (`closed=true`: also closed ones) |
| `show_task` | `GET /api/tasks/{id}` | A task with its sources, conversation, agent work and history |
| `list_projects` | `GET /api/projects` | Projects with open task counts |
| `show_project` | `GET /api/projects/{id}` | A project and its tasks |
| `search` | `GET /api/search?q=` | Tasks, projects and material containing the words |
| `show_delivery` | `GET /api/deliveries/{id}` | A version an agent delivered (files: `/api/deliveries/{id}/files/{n}`) |
| `act_on_task` | `POST /api/tasks/{id}/act` | A button of the page: `{"action": "done"}`, see below |
| `comment_on_task` | `POST /api/tasks/{id}/comment` | `{"text": "...", "to": "hermes" \| "agent"}`, as a Notion comment |
| `create_task` | `POST /api/tasks` | `{"title", "project", "set": {...}, "evidence": [...]}` |
| `update_task` | `PATCH /api/tasks/{id}` | `{"set": {...}, "note"}` |
| `create_project` | `POST /api/projects` | `{"title", "set": {...}, "repos": [...]}` |
| `update_project` | `PATCH /api/projects/{id}` | `{"set": {...}, "repos_add", "repos_remove"}` |
| `record_feedback` | `POST /api/feedback` | `{"said": "...", "scope": "task:<id>"}`: for the next feedback run to interpret |
| `list_delegations` | `GET /api/delegations` | Agent work in progress |
| `show_delegation` | `GET /api/delegations/{id}` | Messages, sub-cards, deliveries, approvals |
| `create_delegation` | `POST /api/delegations` | `{"task", "brief", "allow": ["read", "analyze", "draft"]}` |
| `message_delegation` | `POST /api/delegations/{id}/messages` | `{"kind": "supplement" \| "revision" \| "pause" \| "resume" \| "cancel" \| "accept", "body"}` |
| `answer_delegation` | `POST /api/delegations/{id}/answer` | `{"body", "approve": ["send_email:to Alice only"], "question"}` |
| `approve_delivery` | `POST /api/delegations/{id}/approve` | `{"delivery", "action", "detail"}` |
| `search_sources` | `GET /api/sources?q=&mine=&since=&limit=` | Mail, Slack and GitHub material containing the words |
| `show_source` | `GET /api/sources/{id}` | One item with its full text |
| `status` | `GET /api/status?details=` | Counts, waiting material, agent work, last runs, problems |

`act_on_task` actions: `done`, `todo` (mine), `cancel`, `reopen`, `agent` (hand to agent work),
`wait` (`params.on`, `params.follow_up`), `snooze` (`params.until`), `due` (`params.date`),
`follow_up` (`params.date`), `focus` (`params.on`), `project` (`params.project`), `pause`,
`resume`.

Fields in `set`, as in `todo-manager task update --set`: task `title`, `project_id`, `status`,
`next_actor`, `waiting_on`, `responsibility`, `next_step`, `done_criteria`, `summary`, `due_date`,
`due_basis`, `follow_up_date`, `snooze_until`, `focus`, `focus_reason`, `decision`; project
`title`, `domain`, `status`, `goal`, `next_step`, `blockers`, `icon`. `null` clears a field.

Reads of tasks and projects are the page's cards: dates there are `due` and `follow_up`; writes
name them `due_date` and `follow_up_date`. The material, delegation and status reads are what
`todo-manager source show`, `delegate show` and `status` print.

## Errors

| Status | Body |
|---|---|
| 401 | `{"error": "LOGIN"}`: no token or password, or not the current one |
| 400, 403, 404, 409 | `{"error": "<CODE>", "details": [...]}`, as the command line prints it (409: e.g. a request id reused for other words) |
| 503 | `{"error": "BUSY"}`: the workspace is being installed or restored |

The page's own writes (`act`, `comment`) answer `{"error", "message"}`. A write that partly applied
answers 200 with the refused fields under `rejected`.

## MCP

`POST /mcp` is an MCP server (Streamable HTTP, JSON responses) with the tools above, signed in
with the same Bearer token or password.

Claude Code:

```bash
claude mcp add --transport http todo-manager https://todo.example.com/mcp \
  --header "Authorization: Bearer $TODO_MANAGER_TOKEN"
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.todo-manager]
url = "https://todo.example.com/mcp"
bearer_token_env_var = "TODO_MANAGER_TOKEN"
```

Cursor (`~/.cursor/mcp.json`) and other clients that take headers:

```json
{"mcpServers": {"todo-manager": {"url": "https://todo.example.com/mcp",
                                 "headers": {"Authorization": "Bearer ${env:TODO_MANAGER_TOKEN}"}}}}
```

Hermes (`config.yaml`):

```yaml
mcp_servers:
  todo-manager:
    url: https://todo.example.com/mcp
    headers:
      Authorization: Bearer ${TODO_MANAGER_TOKEN}
```

Clients that only sign in with OAuth (no header of their own) cannot connect yet.

## Skills

Two skills teach an agent to use Todo Manager, in the [skills](https://github.com/vercel-labs/skills)
format:

- `todo-manager`: the command line, on the machine where Todo Manager runs (`todo-manager install`
  also installs it into Hermes).
- `todo-manager-api`: this API, from anywhere, with `TODO_MANAGER_URL` and `TODO_MANAGER_TOKEN`
  set.

```bash
npx skills add ControlNet/todo-manager --skill todo-manager-api
npx skills add ControlNet/todo-manager --skill todo-manager
```
