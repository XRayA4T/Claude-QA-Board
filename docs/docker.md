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

1. **Open the terminal** at <http://127.0.0.1:7681>. If Claude Code signs in from environment
   variables (next section), it's ready. Otherwise sign in the first time: it shows a link to open in
   your browser. The sign-in is saved in `data/claude-home`, so you only do this once.
2. **Open the board** at `http://127.0.0.1:8787/?key=YOUR_KEY`, add your team on the **Users** tab,
   and pick yourself next to **I am**.
3. **Ask it something:** post a question to `dev-claude`. The watcher delivers it to Claude within a
   few seconds.

### Signing in with environment variables (`docker/claude.env`)

If your Anthropic settings are in environment variables (an API key, a subscription token, Amazon
Bedrock, Google Vertex AI, or a company gateway), give them to the Claude container instead of signing
in by hand:

```bash
cd docker
cp claude.env.example claude.env && chmod 600 claude.env
$EDITOR claude.env          # keep one section
docker compose up -d        # creates the container again with the new settings
```

`compose.yaml` loads `claude.env` into the Claude container only, if the file exists. The board never
sees it. The file is git-ignored.

| Signing in with | Put in `claude.env` |
|---|---|
| An API key (Claude Console) | `ANTHROPIC_API_KEY=sk-ant-…` |
| A Claude subscription (Pro, Max, Team, Enterprise) | `CLAUDE_CODE_OAUTH_TOKEN=…`, made once on any computer with `claude setup-token` |
| Amazon Bedrock | `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION=…`, plus access keys, `AWS_BEARER_TOKEN_BEDROCK`, or `AWS_PROFILE` with `~/.aws` mounted |
| Google Vertex AI | `CLAUDE_CODE_USE_VERTEX=1`, `CLOUD_ML_REGION=…`, `ANTHROPIC_VERTEX_PROJECT_ID=…`, `GOOGLE_APPLICATION_CREDENTIALS=` a key file inside the container |
| A gateway or proxy in front of the API | `ANTHROPIC_BASE_URL=…`, `ANTHROPIC_AUTH_TOKEN=…` |
| A specific model (with any of the above) | `ANTHROPIC_MODEL=…` |

**Keeping secrets out of the file:** a line with only a name, such as `ANTHROPIC_API_KEY`, passes
that variable through from the shell you run `docker compose` in. Then nothing secret is written in
`claude.env`:

```bash
# claude.env
ANTHROPIC_API_KEY
ANTHROPIC_MODEL=sonnet          # an alias (sonnet, opus) or a full model name

# your shell (e.g. from a password manager or ~/.bashrc)
export ANTHROPIC_API_KEY=sk-ant-…
docker compose up -d
```

**Credential files** (an AWS profile, a Google key file) need a volume. `compose.yaml` has commented
examples under the Claude service's `volumes:`. Alternatively, put the file in `data/claude-home`,
which is `/home/claude` inside the container.

What the container does with these settings:

- The tmux session, Claude Code and every restart of it inherit them. You don't need to export
  anything in the terminal.
- When it finds one of these settings, the entrypoint skips Claude Code's first-run screens. For
  `ANTHROPIC_API_KEY`, it also accepts the key up front; otherwise Claude Code asks once whether to
  use it. The container log says which method is in use:
  `docker compose logs claude | head -3`.
- After changing `claude.env` or the shell variables, run `docker compose up -d`. `docker compose
  restart` keeps the old values.

Anyone who can run `docker` commands on the host can read these values with `docker inspect`, and so
can Claude itself, from inside the container. Use a key meant for this use, and limit its spend.

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
| `TZ` | `UTC` | The Claude container's timezone, e.g. `Africa/Johannesburg`, so times (and the board's deployment times) are local |
| `NODE_OPTIONS` | `--max-old-space-size=8192` | Node's heap: the ~2 GB default runs out on large test suites |

**Claude's scratchpad survives restarts:** `/tmp` in the Claude container is `data/claude-tmp`, so screenshots,
scripts, git worktrees and other work Claude keeps under `/tmp` are still there after a restart or rebuild.
Clear old files from it now and then.

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
ENVFILE=(); [ -f "$HOME/claude-state/claude.env" ] && ENVFILE=(--env-file "$HOME/claude-state/claude.env")
# /tmp survives restarts (Claude's scratchpad is under it); files untouched for 14 days are cleared
mkdir -p "$HOME/claude-state/tmp" && chmod 1777 "$HOME/claude-state/tmp" 2>/dev/null
find "$HOME/claude-state/tmp" -mindepth 1 -type f -mtime +14 -delete 2>/dev/null
find "$HOME/claude-state/tmp" -mindepth 2 -type d -empty -delete 2>/dev/null
exec docker run --rm -it --init \
  --name "claude-$(basename "$PWD")-$$" \
  --shm-size=2g \
  -v "$PWD:$PWD" -w "$PWD" \
  -v "$HOME/claude-state:/home/claude" \
  -v "$HOME/claude-state/tmp:/tmp" \
  -e TZ="${CLAUDE_TZ:-Africa/Johannesburg}" \
  -e NODE_OPTIONS="${CLAUDE_NODE_OPTIONS:---max-old-space-size=8192}" \
  "${ENVFILE[@]}" \
  -e TERM -e COLORTERM \
  --entrypoint claude \
  qaboard-claude "$@"
```

- **Timezone:** `TZ` gives the container local time (the image has `tzdata`), so it doesn't come back as
  UTC after every restart. A test run that needs UTC can still set it for itself (`TZ=UTC npm test`).
- **Node heap:** `NODE_OPTIONS` raises node's ~2 GB default heap, which large test suites run out of. The
  container has no memory limit here, so it can use what the server has.
- **Scratchpad:** `/tmp` is a folder on the host, so Claude's scratchpad (`/tmp/claude-UID/...`), git
  worktrees and screenshots survive a restart. The launcher clears files older than 14 days.

Anthropic settings in environment variables work the same way as in Compose: put them in
`~/claude-state/claude.env`, in the same format as `docker/claude.env.example`. `docker run --env-file`
also passes through lines that are only a name. The web terminal starts the launcher from cron, which
has almost no environment, so a name-only line there needs the variable set where the launcher runs.
In practice, write the value in the file, or export it at the top of the launcher. With `--entrypoint
claude` there's no automatic acceptance of an API key: answer Claude Code's one-time question in the
terminal.

- **`--init` matters.** Without it Claude Code is process 1 in the container and never cleans up
  finished child processes. Headless browsers in particular then pile up until nothing new can start.
- **Paths:** the project is mounted at the same path as on the host, so `.claude/settings.local.json`
  and the project-scoped MCP entry match.
