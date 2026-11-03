# Running it in Docker

There are two ways to run QA Board with containers. Pick one per project.

| | **A. Docker Compose (two containers)** | **B. Board container + Claude container per session** |
|---|---|---|
| Board | `board` container | `qaboard-NAME` container |
| Claude Code | `claude` container, always running | A new container each time Claude starts (a launcher script) |
| Web terminal and watcher | Inside the `claude` container | On the host (ttyd + tmux + cron) |
| Set up with | `docker compose up -d` in `docker/` | `scripts/setup-environment.sh` over SSH ([setup-server.md](setup-server.md)) |
| Good for | One machine, everything in Docker, quick to try or move | A shared server you manage from your laptop, several projects |

Both keep everything on the machine's `127.0.0.1`. Other people reach it through an SSH tunnel; see
[team-setup.md](team-setup.md).

---

## A. Docker Compose: a board container and a Claude Code container

```
 this machine (127.0.0.1)                     docker network
 ─────────────────────────                     ──────────────
 :8787  board page, MCP  ───────────────────►  board   python:3.12-alpine running server/qaboard.py
                                                        data in docker/data/board (qaboard.db, docs/)
 :7681  web terminal  ──────────────────────►  claude  ttyd ─► tmux session "claude" ─► claude-loop.sh ─► Claude Code
                                                        board watcher (types board messages into the session)
                                                        your project at /workspace
                                                        sign-in, settings and history in docker/data/claude-home
                                                        reaches the board at http://board:8787
```

The web terminal, Claude Code and the watcher share one container on purpose. The terminal has to
show the same tmux session that Claude runs in, and the watcher has to read and type into it. Keeping
them together means no container needs the Docker socket, which would give it root on the host.

### What's in the Claude container

- **Image:** `node:22-bookworm` with Claude Code, git, tmux, python3, ripgrep, jq, ssh, and ttyd 1.7.7 (pinned, checksum checked). Set `WITH_CHROMIUM=1` to add a headless Chromium.
- **User:** a `claude` user with your UID and GID, so files it writes in your project belong to you.
- **On every start**, the entrypoint:
  1. connects Claude Code to the board: the `qaboard` MCP server, permission rules, and the inbox hook
  2. starts the tmux session running Claude Code in `/workspace`
  3. starts the board watcher
  4. runs the web terminal
- **Restart loop:** when Claude exits, it starts again after 5 seconds, continuing the same conversation, with a message telling it to redo its start-up tasks (`.claude/startup.md`). Press Ctrl-C during the countdown to get a shell instead.
- **`init: true`:** finished processes (for example headless browsers) are cleaned up, so they can't fill up the container.

### Set up

You need Docker with the compose plugin, and an x86_64 machine. On arm64, change the ttyd URL and
checksum in `docker/claude/Dockerfile` to the `ttyd.aarch64` release file.

```bash
git clone https://github.com/XRayA4T/Claude-QA-Board.git
cd Claude-QA-Board/docker
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'    # a board key
$EDITOR .env           # BOARD_KEY, BOARD_NAME, PROJECT_DIR (your project), UID/GID (id -u / id -g)
mkdir -p data/board data/claude-home
docker compose up -d --build
```

Then:

1. **Open the terminal** at <http://127.0.0.1:7681> and sign in to Claude Code the first time. It
   shows a link to open in your browser. The sign-in is saved in `data/claude-home`, so you only do
   this once.
2. **Open the board** at `http://127.0.0.1:8787/?key=YOUR_KEY`, add your team on the **Users** tab,
   and pick yourself next to **I am**.
3. **Ask it something:** post a question to `dev-claude`. The watcher delivers it to Claude within a
   few seconds.

### Settings (`docker/.env`)

| Setting | Default | What it does |
|---|---|---|
| `ENV_NAME` | `main` | Names the containers (`qaboard-main-board-1`, …). Use a different one per project. |
| `BOARD_KEY` | — | The board's shared secret. Required. |
| `BOARD_NAME`, `BOARD_ACCENT` | `Team`, `#4f46e5` | Shown on the page |
| `BOARD_PORT`, `TERMINAL_PORT` | `8787`, `7681` | Ports on this machine's 127.0.0.1 |
| `MAX_DOC_MB`, `BOARD_MEMORY` | `50`, `512m` | Largest upload. Set `BOARD_MEMORY=1g` above 60 MB. |
| `BRAND_DIR` | `../brand/example` | A folder with `brand.css` ([branding.md](branding.md)) |
| `PROJECT_DIR` | — | Your project, mounted at `/workspace`. Required. |
| `DEV_WHO` | `dev-claude` | Claude's name on the board |
| `CLAUDE_ARGS` | — | Extra arguments for `claude`, e.g. `--dangerously-skip-permissions` |
| `TTYD_CREDENTIAL` | — | `user:password` to protect the web terminal with a login |
| `WATCH`, `WATCH_POLL`, `WATCH_IDLE`, `WATCH_PROMPT_CHECK`, `WATCH_LIVE` | `1`, `5`, `20`, `1`, `1` | The board watcher ([terminal-and-watcher.md](terminal-and-watcher.md)) |
| `WITH_CHROMIUM` | `0` | `1` adds a headless browser to the image. Rebuild after changing it. |
| `UID`, `GID` | `1000` | Who owns files the containers write |

**Your app's dev server:** add its port under `ports:` of the `claude` service in `compose.yaml`, for
example `- "127.0.0.1:3000:3000"`. The dev server must listen on `0.0.0.0` inside the container.

**More than one project on one machine:** use one copy of the `docker/` folder per project, each with
its own `.env`: a different `ENV_NAME`, `BOARD_PORT` and `TERMINAL_PORT`.

**Tools inside the container (gh, AWS CLI):** add them to `docker/claude/Dockerfile` and rebuild. Their
sign-ins live under `/home/claude`, which is `data/claude-home`, so they survive rebuilds. See
[aws.md](aws.md) and [github.md](github.md).

### Day to day

```bash
cd docker
docker compose ps                                            # what's running
docker compose logs -f board                                 # board requests
docker compose exec claude bash -c 'tail -f ~/.qaboard-watch/watch.log'   # what the watcher delivered
docker compose exec claude tmux -L qaboard attach -t claude  # Claude's session in your terminal (detach: Ctrl-b d)
git pull && docker compose up -d --build                     # update board, watcher and Claude Code
docker compose restart claude                                # fresh container; Claude continues its conversation
docker compose down                                          # stop (data stays in docker/data)
```

Back up `docker/data/board` (the board's questions and documents). `docker/data/claude-home` holds
Claude's sign-in and history. Both folders are git-ignored, and so is `docker/.env`.

### Reaching it from other computers

Nothing listens beyond `127.0.0.1`. From another computer, open an SSH tunnel to this machine that
forwards the two ports, and add any app ports you published:

```bash
ssh -N -L 8787:127.0.0.1:8787 -L 7681:127.0.0.1:7681 you@this-machine
```

The board and terminal then work on that computer's `127.0.0.1` too. To keep the tunnel up, use
`scripts/setup-tunnel.sh` with a `config/envs/NAME.env` that has the same ports
([team-setup.md](team-setup.md)). To connect Claude Desktop, point the bridge at
`http://127.0.0.1:8787/mcp?who=alice-claude&key=KEY`; that page shows the JSON to use.

---

## B. Board container + a Claude Code container per session

This is what `scripts/` sets up on a server ([setup-server.md](setup-server.md)):

- The board runs in its own container, `qaboard-NAME`, started by `deploy-board.sh`.
- The web terminal (ttyd and tmux) and the watcher run on the host.
- Claude Code can run directly on the host (`CLAUDE_CMD="claude"`) or in a new container each time
  it starts, through a small launcher script.

For the container version, set these in `config/envs/NAME.env`:

```bash
CLAUDE_CMD="/home/claude/bin/claude-box"        # the launcher below
CLAUDE_IN_CONTAINER=1                           # Claude reaches the board at the Docker gateway
CLAUDE_JSON="/home/claude/claude-state/.claude.json"
```

A launcher that does this. Build the image from `docker/claude/Dockerfile`, or your own; it only needs
Claude Code in it.

```bash
#!/usr/bin/env bash
# claude-box - run Claude Code for the current folder in a fresh container
exec docker run --rm -it --init \
  --name "claude-$(basename "$PWD")-$$" \
  --shm-size=2g \
  -v "$PWD:$PWD" -w "$PWD" \
  -v "$HOME/claude-state:/home/claude" \
  -e TERM -e COLORTERM \
  --entrypoint claude \
  qaboard-claude "$@"
```

- **`--init` matters.** Without it Claude Code is process 1 in the container and never cleans up
  finished child processes. Headless browsers in particular then pile up until nothing new can start.
- **Paths:** the project is mounted at the same path as on the host, so `.claude/settings.local.json`
  and the project-scoped MCP entry match.
