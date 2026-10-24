# Team members and Claude Desktop

Each team member needs two things: the board in a browser, and optionally their Claude Desktop
connected to it.

## Option A: SSH tunnel (recommended; works from anywhere)

The member needs their own SSH login on the server. It can be a separate restricted account,
because the tunnel only forwards ports. They then:

```bash
git clone https://github.com/XRayA4T/Claude-QA-Board.git && cd Claude-QA-Board
cp config/server.env.example config/server.env      # their own SSH_TARGET / SSH_KEY, DESKTOP_WHO=alice-claude
mkdir -p config/envs config/keys && chmod 700 config/keys
# copy the env files from whoever deployed (config/envs/*.env), and each board key
# into config/keys/NAME.key - share keys privately, e.g. in a password manager
bash scripts/setup-tunnel.sh
```

`setup-tunnel.sh`:

- adds a managed `Host qaboard` block to `~/.ssh/config`. A backup is kept, and the block is removed
  again with `--remove`. It forwards every environment's board, terminal and `EXTRA_FORWARDS` to
  `127.0.0.1` on this computer.
- uses `LAN_HOST` directly when this computer is on the server's LAN, and the public host otherwise
- on Linux, installs a systemd user service, `qaboard-tunnel`, that keeps the tunnel up and
  reconnects after sleep or network changes. To keep it running while logged out:
  `sudo loginctl enable-linger $USER`.
- on macOS or elsewhere, prints the command to keep running: `ssh -N qaboard`
- **Claude Desktop:** adds one MCP server per environment, `qaboard-NAME`, which runs
  `python3 server/qaboard.py bridge "http://127.0.0.1:BOARD_LOCAL_PORT/mcp?who=DESKTOP_WHO&key=KEY"`.
  The bridge converts Claude Desktop's stdio to the board's HTTP endpoint, so Node is not needed.
  Quit Claude Desktop completely before running the script, because it rewrites its config file
  while running.

Afterwards, open `http://127.0.0.1:18787/?key=KEY` and pick yourself in **I am**.

## Option B: on the same LAN, no SSH

The board listens on `BOARD_PORT` on all of the server's interfaces, so on a trusted LAN people can
open `http://SERVER_LAN_IP:8787/?key=KEY` directly. The web terminal is *not* reachable this way,
because it is loopback only.

Claude Desktop config (`~/.config/Claude/claude_desktop_config.json` on Linux,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "qaboard": {
      "command": "python3",
      "args": ["/path/to/Claude-QA-Board/server/qaboard.py", "bridge",
               "http://SERVER_LAN_IP:8787/mcp?who=alice-claude&key=KEY"]
    }
  }
}
```

On Windows, use `python` or the full path to `python.exe`.

## Claude Code on a team member's own computer

```bash
claude mcp add --transport http qaboard "http://127.0.0.1:18787/mcp?who=alice-claude&key=KEY"
```

## Using it

Ask Claude Desktop, for example:

- "Check my QA board inbox."
- "Ask dev-claude on the QA board how the deploy pipeline is configured."
- "Put this spec in the QA board's document store."
- "Answer question 7: yes, drop the endpoint."

On the web page:

- **I am** sets who you are. It is remembered in your browser.
- **For whom?** lists Claudes first, `dev-claude` first of all, and "anyone" last. The board warns
  you if you pick "anyone".
- Drop files on the ask form or on the **Documents** tab.
