# Server setup

This guide sets up one **environment**: one project on the server with its own QA board, its own
Claude Code web terminal and its own board watcher. You can repeat it for more projects. Each one
gets its own ports, and they all share one SSH tunnel.

You run everything **from your own computer** in a clone of this repo. The scripts connect to the
server over SSH. Nothing needs to be cloned on the server.

## 1. Prepare the server (once, needs an admin)

On the server, as an admin:

```bash
sudo apt install docker.io tmux cron util-linux python3     # or your distro's equivalents
sudo systemctl enable --now docker cron
sudo adduser claude                                          # the account everything runs as
sudo usermod -aG docker claude
```

Then, as that account:

- Install Claude Code ([docs](https://docs.claude.com/en/docs/claude-code/overview)) and sign in
  once by running `claude` in a terminal. If you prefer to run Claude Code in a container, see
  [claude-code.md](claude-code.md).
- Put your project somewhere, for example `/home/claude/projects/webapp`.
- Add your SSH public key to `~/.ssh/authorized_keys`.

> **Why a separate account?** Everything runs unprivileged, so the board, the terminal and Claude
> Code can only touch that account's files. Membership of the `docker` group is equivalent to root
> on that machine, so use a server you trust everyone with access to.

## 2. Configure (on your computer)

```bash
git clone https://github.com/XRayA4T/Claude-QA-Board.git
cd Claude-QA-Board
cp config/server.env.example config/server.env
cp config/envs/example.env.example config/envs/webapp.env     # "webapp" = the environment's name
```

Edit **`config/server.env`**:

| Setting | Meaning |
|---|---|
| `SSH_TARGET` | `account@host` of the server account, for example `claude@dev.example.com` |
| `SSH_KEY` | Private key for that login. It must not prompt for a passphrase, or must be loaded in ssh-agent. |
| `LAN_HOST`, `LAN_PREFIX` | Optional. When your computer's IP matches `LAN_PREFIX`, the tunnel connects to `LAN_HOST` directly. |
| `REMOTE_BASE` | Where everything is installed on the server. Empty means `~/qaboard`. |
| `DOCKER_GATEWAY` | How containers on the server reach host ports. The default is `172.17.0.1`. |
| `DESKTOP_WHO` | The name your Claude Desktop uses on the boards. |

Edit **`config/envs/webapp.env`**. The most important settings are:

| Setting | Meaning |
|---|---|
| `BOARD_NAME`, `BOARD_ACCENT` | Shown on the page |
| `BOARD_PORT` / `BOARD_LOCAL_PORT` | The board's port on the server and on your computer (through the tunnel) |
| `PROJECT_DIR` | The project folder on the server |
| `CLAUDE_CMD` | What the web terminal runs in `PROJECT_DIR`. Usually `claude`. |
| `TERMINAL_PORT` / `TERMINAL_LOCAL_PORT` | The web terminal's port on the server (loopback only) and on your computer |
| `EXTRA_FORWARDS` | More ports to tunnel, for example your app's dev server: `13000:3000:web-app` |

For a second environment, copy the file again and give it different ports, for example 8788,
18788, 7682 and 17682.

All of `config/` except the `*.example` files is git-ignored. That includes `config/keys/`, where the
board keys are kept.

## 3. Deploy

```bash
bash scripts/setup-environment.sh --env webapp
```

This runs five scripts in order. Each one is safe to re-run on its own:

| Step | Script | What it does |
|---|---|---|
| 1 | `deploy-board.sh` | Makes `config/keys/webapp.key` the first time. Copies `qaboard.py` and the look, then starts the `qaboard-webapp` container (read-only, memory-capped, `--restart unless-stopped`). |
| 2 | `configure-claude-code.sh` | Gives the project's Claude Code a `qaboard` MCP server, permissions and an inbox hook. See [claude-code.md](claude-code.md). |
| 3 | `deploy-web-terminal.sh` | Installs ttyd (version pinned, checksum checked) and the tmux session that runs `CLAUDE_CMD`. Registers both in crontab `@reboot`. |
| 4 | `deploy-board-watch.sh` | Installs the watcher. See [terminal-and-watcher.md](terminal-and-watcher.md). |
| 5 | `setup-tunnel.sh` | Sets up this computer's SSH tunnel for all environments, and Claude Desktop. See [team-setup.md](team-setup.md). |

At the end, the script prints the board link with its key. Open it, go to **Users**, and add your
team (people and their Claudes). `dev-claude` is already listed.

## Day to day

| Task | Command |
|---|---|
| Update the board after `git pull` | `bash scripts/deploy-board.sh --env webapp` |
| Update the watcher | `bash scripts/deploy-board-watch.sh --env webapp` |
| See what the watcher would announce now | `bash scripts/deploy-board-watch.sh --env webapp --check` |
| Board logs (on the server) | `docker logs -f qaboard-webapp` |
| Attach to Claude's session over SSH | `tmux -L qaboard-webapp attach` |
| Back up | `<REMOTE_BASE>/webapp/board/data/` (`qaboard.db` and `docs/`) |
| Remove the terminal or watcher | add `--remove` to `deploy-web-terminal.sh` or `deploy-board-watch.sh` |
| Remove the board | `docker rm -f qaboard-webapp` on the server, then delete the folder if you no longer need the data |

## Server layout

```
<REMOTE_BASE>/webapp/
  board/     qaboard.py, data/qaboard.db, data/docs/<id>/..., data/brand/
  terminal/  run.sh (supervisor), session.sh, inner.sh, tmux.conf, ttyd.log
  watch/     board-watch.py, config.json (contains the key, mode 600), state.json, watch.log
~/.local/bin/ttyd
crontab:    @reboot .../terminal/run.sh   and   @reboot .../watch/run.sh
```
