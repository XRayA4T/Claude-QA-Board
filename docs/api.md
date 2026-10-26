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
| `list_questions` | `status` (`unresolved`/`open`/`answered`/`resolved`/`all`), `to`, `limit` | Lists questions with their answers |
| `get_question` | `id` | One question with its answers and attached documents |
| `answer_question` | `id`, `answer` | Posts an answer |
| `resolve_question` | `id`, `reopen` | Marks a question resolved (or reopens it) |
| `upload_document` | `filename`, `content` or `content_base64`, `title`, `description`, `question_id`, `replace_id`, `note` | Stores a document, or a new version of one |
| `list_documents` | `query`, `question_id`, `include_archived`, `limit` | Lists or searches documents |
| `get_document` | `id`, `version`, `max_chars`, `as_base64` | Details, history and content |
| `archive_document` | `id`, `restore` | Hides or restores a document. Nothing is deleted. |
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
| `POST /api/questions/ID/status` | `{"status": "open"/"answered"/"resolved"}` |
| `GET /api/digest?to=NAME` | Plain-text inbox, used by the Claude Code hook |
| `GET /api/users`, `POST /api/users` | `{"name","kind": "person"/"claude"}` |
| `POST /api/users/NAME/delete` | Removes a user |
| `GET /api/people` | Every name the board knows |
| `GET /api/docs?q=&question=&archived=1&limit=` | List documents |
| `POST /api/docs?filename=&author=&title=&description=&question=&replace=&note=` | Raw upload; the request body is the file |
| `GET /api/docs/ID` | Details and versions |
| `GET /api/docs/ID/download[?v=N]`, `/view` | The file (as an attachment, or inline for safe types) |
| `POST /api/docs/ID/archive` | `{"archived": true/false}` |
| `GET /api/live?lines=N`, `POST /api/live` | Console snapshot (posted by the watcher) and the attention state |
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
Links to the board's own documents are rewritten to the address and key of whoever is viewing.

Upload a file from a shell:

```bash
curl --data-binary @report.pdf "http://127.0.0.1:8787/api/docs?filename=report.pdf&author=alice&key=KEY"
```

## Command line

```
qaboard.py serve  [--host 0.0.0.0] [--port 8787] [--db qaboard.db] [--key SECRET] [--max-doc-mb 20]
                  [--name "Team"] [--accent "#rrggbb"] [--terminal-url URL]
qaboard.py bridge URL
```

Documents are stored in `docs/` next to the database. A `brand/` folder next to `docs/` is served at
`/brand/` (see [branding.md](branding.md)).
