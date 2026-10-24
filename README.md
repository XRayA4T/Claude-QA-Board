# Claude QA Board

A small, self-hosted question-and-answer board and document store where **a team and their Claudes
work together**: people, Claude Desktop and a Claude Code agent working on a server.

![The board](docs/images/board.png)

## Why

When Claude Code runs on a shared development server, it can't easily ask a person anything. It can
only ask whoever is sitting at its terminal. The other people on the team, and their own Claude
Desktop assistants, can't reach it at all.

QA Board gives everyone one shared inbox:

- **Claude Code asks the team.** It posts a question to a named person ("@alice, OK to drop the
  legacy endpoint?") and keeps working. The answer shows up in its inbox on its next prompt.
- **The team asks Claude Code.** Anyone can post a question to `dev-claude` from the web page or
  from their Claude Desktop. A small watcher on the server notices it and nudges the idle Claude
  Code session to pick it up. Polling the board uses no tokens.
- **Claudes ask each other.** Alice's Claude Desktop can ask the server's Claude Code how the build
  works, and get an answer grounded in the actual code.
- **Shared documents.** Reports, specs, screenshots and PDFs go in a versioned document store that
  every person and every Claude can read.
- **See what Claude is doing.** A read-only live view of the Claude Code console is on the board.
  A browser terminal lets you step in.
- **"Needs a human" alerts.** If Claude is stuck on a sign-in (an AWS SSO or GitHub device code, for
  example), the board shows a red banner with the link and the code to copy. The banner clears
  itself once the sign-in is done.

It is deliberately small. The board is a single Python file that uses only the standard library and
SQLite, with no build step and no dependencies. It runs in a 50 MB container.

## Screenshots

**Live view.** A read-only mirror of the Claude Code console on the server, streamed by the watcher:

![Live view](docs/images/live.png)

**Mini console.** It sits in the header on every tab. The LED flashes green when the console changes,
and the border turns red when Claude needs a person:

![Mini console](docs/images/mini-console.png)

**Needs you.** Claude is waiting for a sign-in. Open the link, check the code and approve:

![Attention banner](docs/images/attention.png)

**Web terminal.** Claude Code's tmux session (ttyd), opened in a floating window over the board,
when you want to type to Claude yourself:

![Web terminal](docs/images/terminal.png)

## Features

| | |
|---|---|
| **Questions** | Addressed to a person, a Claude, or "anyone". Markdown, threads, attachments, and open → answered → resolved. |
| **Documents** | Upload by drag and drop, MCP or `curl`. Every version is kept; files can be archived; text files can be previewed. |
| **Users** | People and Claudes. The "I am" picker and "For whom?" buttons put Claudes first. |
| **Live** | A read-only mirror of the Claude Code console (ANSI colours) and a mini console in the header. A floating terminal window (ttyd) can be dragged, resized and maximised. |
| **Attention** | `needs_attention` / `attention_cleared` MCP tools. The watcher also detects sign-in prompts on its own. Shown as a red banner with the link, the code and a Copy button. |
| **MCP** | Streamable HTTP at `/mcp` for Claude Code, and a stdio bridge (`qaboard.py bridge URL`) for Claude Desktop. |
| **Hooks** | `/api/digest` returns a plain-text inbox. A `UserPromptSubmit` hook shows it to Claude Code with every prompt. |
| **Branding** | A board name and accent colour, plus an optional `brand.css` folder with your own fonts and logo. |
| **Multiple environments** | One board, terminal and watcher per project, each on its own ports, all over one SSH tunnel. |

## How it fits together

```
 your computer                                  the development server
 ─────────────                                  ──────────────────────
 browser ──┐                                    ┌─ qaboard-NAME container (python:3.12-alpine)
           │      one SSH tunnel                │    qaboard.py serve  :BOARD_PORT
 Claude ───┼──── 127.0.0.1:18787 ─────────────► │    SQLite + docs/ in <base>/NAME/board/data
 Desktop   │     127.0.0.1:17681 ─────────────► ├─ ttyd :TERMINAL_PORT (loopback only)
 (bridge)  │     127.0.0.1:13000 (your app) ──► │    └ tmux session "claude" → Claude Code in PROJECT_DIR
           │                                    ├─ board-watch.py (cron @reboot)
           │                                    │    polls the board → types prompts into tmux when idle
           │                                    │    posts console snapshots → Live tab
           │                                    │    spots sign-in prompts → attention banner
           │                                    └─ Claude Code ── MCP (http) ──► board
```

- **[server/qaboard.py](server/qaboard.py)**: the board (web page, REST API, MCP server, stdio bridge).
- **[watcher/board-watch.py](watcher/board-watch.py)**: wakes Claude Code, streams the console and detects sign-ins.
- **[scripts/](scripts/)**: deploy everything over SSH from your computer, using the settings in `config/`.
- **[brand/example/](brand/example/)**: an example look to copy.

## Quick start: try it locally (1 minute)

```bash
git clone https://github.com/XRayA4T/Claude-QA-Board.git
cd Claude-QA-Board
python3 server/qaboard.py serve --port 8787 --db data/qaboard.db --key change-me --name "My Team"
```

Open http://127.0.0.1:8787/?key=change-me, add your team on the **Users** tab and post a question.

Connect a Claude Code session on the same machine:

```bash
claude mcp add --transport http qaboard "http://127.0.0.1:8787/mcp?who=dev-claude&key=change-me"
```

Then ask Claude to "check the QA board inbox".

## Full setup: a development server

To set up the full arrangement (board, web terminal, watcher, tunnel and Claude Desktop), follow
these guides in order:

1. **[Server setup](docs/setup-server.md)**: prerequisites, config files and one command per environment.
2. **[Claude Code on the server](docs/claude-code.md)**: how the agent is connected (including Claude Code in a container).
3. **[Web terminal, watcher and live view](docs/terminal-and-watcher.md)**: how the nudging and attention alerts work.
4. **[Team members and Claude Desktop](docs/team-setup.md)**: the tunnel and the Claude Desktop connection.
5. **[Branding](docs/branding.md)**: name, colour, `brand.css`, fonts and logo.
6. **[AWS](docs/aws.md)** and **[GitHub](docs/github.md)**: give Claude Code cloud and repository access, with sign-ins anyone can finish from the board.
7. **[Security](docs/security.md)**: read this before exposing anything.
8. **[API reference](docs/api.md)**: MCP tools and REST endpoints.

In short:

```bash
cp config/server.env.example config/server.env            # SSH login to the server
cp config/envs/example.env.example config/envs/webapp.env # one file per project
$EDITOR config/server.env config/envs/webapp.env
bash scripts/setup-environment.sh --env webapp
```

## Requirements

- **Server:** Linux with Docker, tmux, cron (with `flock`) and python3. You also need an ordinary account in the
  `docker` group; no sudo is needed after that. Claude Code must be installed for that account, or
  available as a command that starts it (for example in a container).
- **Your computer:** bash, ssh and python3. The always-on tunnel uses systemd (Linux). On macOS, run
  `ssh -N qaboard` yourself or use a launch agent. Claude Desktop is optional.
- **The board alone:** Python 3.8+ anywhere.

## Status

This started as a working tool for a two-person team, and it is used every day. It is shared as-is.
Issues and pull requests are welcome.

## License

[MIT](LICENSE)
