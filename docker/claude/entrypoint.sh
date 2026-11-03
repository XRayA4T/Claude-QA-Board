#!/usr/bin/env bash
# entrypoint.sh - start the Claude container:
#   1. connect Claude Code to the board (user-scope MCP server, inbox hook, permissions) - every start,
#      so a changed BOARD_KEY or DEV_WHO is picked up
#   2. start the tmux session "claude" (socket "qaboard") running claude-loop.sh in /workspace
#   3. start the board watcher (WATCH=1), restarted if it stops
#   4. run ttyd in the foreground: every browser tab attaches to the same tmux session
set -euo pipefail
: "${BOARD_URL:?}" "${BOARD_KEY:?set BOARD_KEY}" "${DEV_WHO:=dev-claude}"
mkdir -p "$HOME/.claude" "$HOME/.qaboard-watch"

# 1. Claude Code -> board
python3 - "$HOME" "$BOARD_URL" "$BOARD_KEY" "$DEV_WHO" <<'PY'
import json, os, sys
home, board, key, who = sys.argv[1:]

def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}

def save(path, data):
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)

cj = os.path.join(home, ".claude.json")          # user-scope MCP servers live here
d = load(cj)
d.setdefault("mcpServers", {})["qaboard"] = {"type": "http", "url": "%s/mcp?who=%s&key=%s" % (board, who, key)}
save(cj, d)

sp = os.path.join(home, ".claude", "settings.json")
s = load(sp)
allow = s.setdefault("permissions", {}).setdefault("allow", [])
for rule in ["mcp__qaboard"] + ["Bash(curl %s--data-binary @* %s%s/api/docs*)" % (f, q, board)
                                for f in ("", "-s ") for q in ("", "'", '"')]:
    if rule not in allow:
        allow.append(rule)
hooks = s.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
for g in hooks:
    g["hooks"] = [h for h in g.get("hooks", []) if "/api/digest" not in h.get("command", "")]
hooks[:] = [g for g in hooks if g.get("hooks")]
hooks.append({"hooks": [{"type": "command",
    "command": "curl -s --max-time 2 '%s/api/digest?to=%s&key=%s' || true" % (board, who, key)}]})
save(sp, s)
print("Claude Code connected to %s as %s" % (board, who))
PY

# 2. the shared tmux session (created now, so Claude runs before anyone opens the terminal)
TMUX_ARGS=(-u -L qaboard -f /opt/claude/tmux.conf)
tmux "${TMUX_ARGS[@]}" kill-server 2>/dev/null || true
# shellcheck disable=SC2086
tmux "${TMUX_ARGS[@]}" new-session -d -s claude -x 200 -y 50 -c /workspace /opt/claude/claude-loop.sh ${CLAUDE_ARGS:-}
tmux "${TMUX_ARGS[@]}" set -g status-left " ${TERMINAL_TITLE:-claude} "

# 3. the board watcher (plain HTTP polling, no tokens)
if [ "${WATCH:-1}" = 1 ]; then
  W="$HOME/.qaboard-watch"
  cp /opt/claude/board-watch.py "$W/board-watch.py"
  python3 - "$W/config.json" <<PY
import json, os, sys
e = os.environ
json.dump({"board": e["BOARD_URL"], "key": e["BOARD_KEY"], "who": e.get("DEV_WHO", "dev-claude"),
           "tmux_socket": "qaboard", "tmux_session": "claude",
           "poll": int(e.get("WATCH_POLL", 5)), "idle": int(e.get("WATCH_IDLE", 20)),
           "prompt_check": e.get("WATCH_PROMPT_CHECK", "1") == "1", "live": e.get("WATCH_LIVE", "1") == "1",
           "live_interval": 2, "title": e.get("TERMINAL_TITLE", "claude")}, open(sys.argv[1], "w"), indent=1)
PY
  chmod 600 "$W/config.json"
  ( while true; do
      [ -f "$W/watch.log" ] && [ "$(stat -c %s "$W/watch.log")" -gt 2000000 ] && mv -f "$W/watch.log" "$W/watch.log.1"
      python3 -u "$W/board-watch.py" >> "$W/watch.log" 2>&1
      sleep 10
    done ) &
fi

# 4. the web terminal: listens on the container's port 7681 (published on the host's 127.0.0.1 only)
TTYD_OPTS=(-p 7681 -W -O -t "titleFixed=${TERMINAL_TITLE:-claude}" -t fontSize=15 -t disableLeaveAlert=true
           -t 'theme={"background":"#1b1530","foreground":"#ece8f5","cursor":"#a99cf0","selectionBackground":"#2f2650"}')
[ -n "${TTYD_CREDENTIAL:-}" ] && TTYD_OPTS+=(-c "$TTYD_CREDENTIAL")
exec ttyd "${TTYD_OPTS[@]}" tmux "${TMUX_ARGS[@]}" new-session -A -s claude -c /workspace /opt/claude/claude-loop.sh
