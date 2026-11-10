# API reference

Every request needs the board key, as `?key=KEY` or an `X-QA-Key: KEY` header. The caller's name is
`?who=NAME` or an `X-QA-Who` header, and defaults to `claude`.

## MCP

- **HTTP (Claude Code):** `POST /mcp?who=NAME&key=KEY` speaks JSON-RPC 2.0 (the streamable HTTP transport).
- **stdio (Claude Desktop):** `python3 qaboard.py bridge "http://HOST:PORT/mcp?who=NAME&key=KEY"`

| Tool | Arguments | What it does |
|---|---|---|
| `check_inbox` | — | Open questions for you (or anyone), and answers to your own questions |
| `post_question` | `title`, `body`, **`to`** | Asks a person or a Claude. `anyone` only if the user said so. |
| `list_questions` | `status` (`unresolved`/`open`/`answered`/`on_hold`/`resolved`/`all`), `to`, `limit` | Lists questions with their answers. `unresolved` leaves out questions on hold. |
| `get_question` | `id` | One question with its answers and attached documents |
| `answer_question` | `id`, `answer` | Posts an answer |
| `resolve_question` | `id`, `reopen` | Marks a question resolved (or reopens it) |
| `hold_question` | `id`, `release` | Puts a question on hold, or takes it off hold. Held questions drop out of every inbox and the watcher, and Claude is told to leave them alone. |
| `upload_document` | `filename`, `content` or `content_base64`, `title`, `description`, `question_id`, `replace_id`, `note` | Stores a document, or a new version of one |
| `list_documents` | `query`, `question_id`, `include_archived`, `limit` | Lists or searches documents |
| `get_document` | `id`, `version`, `max_chars`, `as_base64` | Details, history and content |
| `archive_document` | `id`, `restore` | Hides or restores a document. Nothing is deleted. |
| `who_is_watching` | — | Who is watching the session from the board (terminal open, Live tab, on the board) and how many other terminals are attached |
| `deployment_progress` | `deployment`, `status`, `target`, `step`, `steps`, `progress`, `questions`, `branches`, `commit`, `started`, `updated`, `finished`, `eta`, `waitingFor`, `error` | Reports a deployment's whole state (see below) |
| `needs_attention` | `reason`, `url`, `code` | Raises the "a person is needed" banner |
| `attention_cleared` | — | Clears it |

The exact schemas are in `TOOLS` in [`server/qaboard.py`](../server/qaboard.py).

## REST

| Method and path | |
|---|---|
| `GET /` | The web page |
| `GET /health` | `{"ok": true, "version": "…"}` |
| `GET /api/questions?status=&to=&limit=` | List questions |
| `GET /api/questions/ID` | One question |
| `POST /api/questions` | `{"title","body","author","to"}` |
| `POST /api/questions/ID/answers` | `{"body","author"}` |
| `POST /api/questions/ID/status` | `{"status": "open"/"answered"/"resolved"/"on_hold"/"reopen"}`. `reopen` sets answered or open, depending on whether it has answers. |
| `GET /api/digest?to=NAME` | Plain-text inbox, used by the Claude Code hook. Ends with who is watching the session ("Watching your session now: raymond (terminal open), lorretta (Live tab)") when anyone is on the board. |
| `GET /api/presence` | Who is on the board now: each person (by the name chosen next to "I am"), whether their terminal window is open, whether they are on the Live tab, and how many other terminals are attached to Claude's session. |
| `POST /api/presence` | A board page checking in (every 15 s): `{id, who, view, term, hidden}`; `gone: true` when it closes. Silent for 75 s counts as gone. |
| `GET /api/users`, `POST /api/users` | `{"name","kind": "person"/"claude"}` |
| `POST /api/users/NAME/delete` | Removes a user |
| `GET /api/people` | Every name the board knows |
| `GET /api/docs?q=&question=&archived=1&limit=` | List documents |
| `POST /api/docs?filename=&author=&title=&description=&question=&replace=&note=` | Raw upload; the request body is the file |
| `GET /api/docs/ID` | Details and versions |
| `GET /api/docs/ID/download[?v=N]`, `/view` | The file (as an attachment, or inline for safe types) |
| `POST /api/docs/ID/archive` | `{"archived": true/false}` |
| `GET /api/live?lines=N`, `POST /api/live` | Console snapshot (posted by the watcher) and the attention state |
| `GET /api/deployments[?limit=20]`, `GET /api/deployments/ID` | Recent deployments, newest first, or one deployment |
| `PUT /api/deployments/ID` (or `POST`) | Report a deployment's whole state. It replaces the earlier report for that ID. |
| `POST /api/attention` | `{"reason","url","code","by"}` or `{"clear": true}` |

Posts (questions and answers) are Markdown: **bold** / __bold__, *italic* / _italic_, ~~strikethrough~~,
`code`, code blocks, lists and task lists (`- [ ]`, `- [x]`), headings, > block quotes, horizontal
rules (`---`), links, `\` escapes and tables (`|---|:---:|---:|` sets left, centre and right alignment). Images from the document store are shown inline, so Claude can show you what
it made:

```markdown
![The new logo](/api/docs/12/view)            the latest version
![The first draft](/api/docs/12/view?v=1)     an older version
```

PNG, JPEG, GIF, WebP and SVG are supported. Images from other sites are shown as links, not loaded.
Markdown documents (`.md`) open in a pop-up on the board page, laid out like a page; HTML
documents open as (sandboxed) pages; other text files open as plain text.
Links to the board's own documents are rewritten to the address and key of whoever is viewing.

Upload a file from a shell:

```bash
curl --data-binary @report.pdf "http://127.0.0.1:8787/api/docs?filename=report.pdf&author=alice&key=KEY"
```

## Settings

| Endpoint | |
|---|---|
| `GET /api/settings` | The branding set on the Settings tab (`name`, `accent`, `css`, `logo`), the board's own (`defaults`) and what is in use (`effective`). |
| `POST /api/settings` | `{name?, accent?, css?, reset?: ["name", "accent", "css", "logo"]}`. An empty value clears that setting. |
| `POST /api/settings/logo` | The image as the body, with its `Content-Type`; an empty body removes the logo. Served at `/brand-logo`. |

## Updating the board

| Endpoint | |
|---|---|
| `GET /api/board/update` (`?check=1` to ask GitHub now) | The version running, the newest on GitHub (checked at most hourly), whether an update is available, what's new, and the state of an update. |
| `POST /api/board/update` | Update now (only with `--self-update`): download, check it compiles and starts, keep `qaboard.py.prev`, replace `qaboard.py` and exit, so the container's restart policy starts the new version. |

To go back: replace `qaboard.py` with `qaboard.py.prev` (in the board's `app/` folder on the server) and restart the container.

## Deployment progress

Claude (with `deployment_progress`) or a script reports a deployment by sending its **whole state**
every time a step starts or finishes. A report replaces the earlier one for the same ID, so a missed
report does no harm. The board shows a progress bar under the tabs on every page while a deployment
is running, waiting, or finished in the last 15 minutes. The full step list is on the **Deploys** tab.

```json
{
  "deployment": "dep-20261104-0933",
  "target": "www-dev",
  "status": "running",
  "step": "pipeline",
  "progress": 0.62,
  "questions": [86, 87],
  "branches": ["fix/font"],
  "commit": "483302e",
  "started": "2026-11-04T09:21:40+02:00",
  "updated": "2026-11-04T09:41:12+02:00",
  "eta": "2026-11-04T09:52:00+02:00",
  "steps": [
    { "id": "tests",    "label": "Server tests", "status": "done", "started": "2026-11-04T09:21:40+02:00",
      "finished": "2026-11-04T09:28:21+02:00", "estimate": 420, "detail": "756 passed" },
    { "id": "pipeline", "label": "AWS pipeline", "status": "running", "started": "2026-11-04T09:28:21+02:00", "estimate": 900,
      "stages": [ { "name": "Build", "status": "done" }, { "name": "Deploy", "status": "running" } ] },
    { "id": "announce", "label": "Answer the questions", "status": "waiting", "estimate": 60 }
  ],
  "waitingFor": null,
  "error": null
}
```

- **`status`** (for the deployment, each step and each stage): `running`, `waiting`, `done` or `failed`.
- **`progress`** (0 to 1) is optional. Without it, the board counts finished steps, and a running
  step's stages count as fractions of that step.
- **Times** are ISO 8601 with the timezone (`date -Iseconds`): `started` for the deployment (keep it the same in
  every report) and each step's `started` and `finished`, kept in every later report. `finished` for the
  deployment when it is done or failed. Times a report leaves out are filled in by the board from the
  earlier report or the time the report arrives, so they are approximate if reports are rare.
- **Elapsed and time left.** The Deploys tab shows when each step started or finished and how long it took
  (or has been running), and for the deployment how long it has been running (or took) and its **ETA**.
  The ETA is the report's `eta`, else worked out from the steps' `estimate` (seconds each step usually takes;
  a waiting step shows it as "~2m 00s", a finished one shows it next to the time it really took), else a
  guess from the average time of the steps done so far ("about 3m left"). The bar under the tabs shows the
  time left too.
- **`waitingFor`**: `{reason, url, code}`, the same as `needs_attention`. It raises the board's
  "Needs you" banner, and sending `null` clears it again.
- **`error`**: `{step, message, log}`. Upload the full log as a document and refer to it as
  `"doc 812"` so it becomes a link.
- **Questions** listed in `questions` link to those questions.
- A running deployment with no report for 15 minutes is shown as "no update".
- Clicking a bar opens that deployment on the Deploys tab. Clicking the **done ✕** (or **failed ✕**)
  label hides a finished deployment from the bar in that browser. It stays on the Deploys tab, and a
  newer report for it shows the bar again.

From a shell:

```bash
curl -X PUT --data-binary @state.json "http://127.0.0.1:8787/api/deployments/dep-20261104-0933?key=KEY"
```

## Command line

```
qaboard.py serve  [--host 0.0.0.0] [--port 8787] [--db qaboard.db] [--key SECRET] [--max-doc-mb 20]
                  [--name "Team"] [--accent "#rrggbb"] [--terminal-url URL]
qaboard.py bridge URL
```

Documents are stored in `docs/` next to the database. A `brand/` folder next to `docs/` is served at
`/brand/` (see [branding.md](branding.md)).
