#!/usr/bin/env bash
# deploy-board-watch.sh - wake an environment's Claude Code when its QA board has something for it,
# stream its console to the board's Live tab, and flag sign-in prompts that need a person.
# Run from your computer, in the repo:
#
#     bash scripts/deploy-board-watch.sh --env NAME            install / update
#     bash scripts/deploy-board-watch.sh --env NAME --check    what would it announce right now?
#     bash scripts/deploy-board-watch.sh --env NAME --remove
#
# On the server (as the SSH account, no sudo) it installs watcher/board-watch.py in
# <REMOTE_BASE>/NAME/watch. Every WATCH_POLL seconds it checks the board (plain HTTP, no tokens).
# When a question for DEV_WHO arrives, or someone replies on one of its threads, it types a one-line
# prompt into the web terminal's tmux session once Claude has been idle for WATCH_IDLE seconds.
# WATCH_PROMPT_CHECK=1: never type while a permission or question prompt is on screen.
# WATCH_LIVE=1: send the console to the board every 2 s for its read-only Live tab - everyone with
#               the board key can then see whatever the console shows.
# Needs the web terminal (deploy-web-terminal.sh): that is the session it watches and types into.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

parse_env_arg "$@"; set -- "${REST[@]+"${REST[@]}"}"
MODE=deploy
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --remove) MODE=remove ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done
load_server; load_env "$ENV_ARG"; remote_dirs
PAT="$ENV_NAME/watch"

if [ "$MODE" = check ]; then
  rsh "python3 '$R_WATCH/board-watch.py' --check; echo; tail -5 '$R_WATCH/watch.log' 2>/dev/null"
  exit 0
fi
if [ "$MODE" = remove ]; then
  rsh "crontab -l 2>/dev/null | grep -vF '$R_WATCH/run.sh' | crontab -" || true
  rsh "pkill -f '$PAT/[r]un.sh'; pkill -f '$PAT/[b]oard-watch.py'; true"
  rsh "rm -rf '$R_WATCH'"
  echo "Removed."
  exit 0
fi

KEY="$(board_key)"
SRC="$REPO/watcher/board-watch.py"
python3 -m py_compile "$SRC" || die "watcher/board-watch.py has a syntax error"

say "1/4 Checking the server"
rsh "command -v tmux >/dev/null && command -v python3 >/dev/null && command -v crontab >/dev/null && command -v flock >/dev/null" \
  || die "the server needs tmux, python3, cron and flock"
rsh "test -d '$R_TERM'" \
  || echo "Note: the web terminal isn't installed yet (deploy-web-terminal.sh --env $ENV_NAME). The watcher waits until its session exists."
code=$(rsh "python3 -c \"import urllib.request as u; print(u.urlopen('http://127.0.0.1:$BOARD_PORT/health?key=$KEY', timeout=5).status)\" 2>/dev/null" || true)
[ "$code" = 200 ] || die "the board didn't answer on the server's 127.0.0.1:$BOARD_PORT with this environment's key (deploy-board.sh first)"
echo "OK."

say "2/4 Installing the watcher in $R_WATCH"
rsh "mkdir -p '$R_WATCH' && chmod 700 '$R_WATCH'"
rsh "cat > '$R_WATCH/board-watch.py.new' && mv '$R_WATCH/board-watch.py.new' '$R_WATCH/board-watch.py' && chmod +x '$R_WATCH/board-watch.py'" < "$SRC"
python3 - "$KEY" "$DEV_WHO" "$WATCH_POLL" "$WATCH_IDLE" "$WATCH_PROMPT_CHECK" "$BOARD_PORT" "$TMUX_SOCK" "$TMUX_SESSION" "$WATCH_LIVE" "claude $ENV_NAME" <<'PY' | rsh "umask 077; cat > '$R_WATCH/config.json'"
import json, sys
key, who, poll, idle, pc, port, sock, sess, live, title = sys.argv[1:]
print(json.dumps({"board": "http://127.0.0.1:" + port, "key": key, "who": who,
                  "tmux_socket": sock, "tmux_session": sess, "poll": int(poll), "idle": int(idle),
                  "prompt_check": pc == "1", "live": live == "1", "live_interval": 2, "title": title}, indent=1))
PY
rsh "cat > '$R_WATCH/run.sh' && chmod +x '$R_WATCH/run.sh'" <<EOF
#!/usr/bin/env bash
# Keeps board-watch.py running; started at boot from crontab (@reboot).
cd '$R_WATCH'
exec 9>'$R_WATCH/.lock'
flock -n 9 || exit 0
export PATH="/usr/local/bin:/usr/bin:/bin"
while true; do
  [ -f watch.log ] && [ "\$(stat -c %s watch.log)" -gt 2000000 ] && mv -f watch.log watch.log.1
  python3 -u '$R_WATCH/board-watch.py' >> watch.log 2>&1
  sleep 10
done
EOF

say "3/4 Starting it, and registering it to start at boot"
rsh "(crontab -l 2>/dev/null | grep -vF '$R_WATCH/run.sh'; echo '@reboot $R_WATCH/run.sh') | crontab -"
rsh "pkill -f '$PAT/[r]un.sh'; pkill -f '$PAT/[b]oard-watch.py'; true"
sleep 1
rsh "nohup setsid '$R_WATCH/run.sh' >/dev/null 2>&1 < /dev/null &"
sleep 3
rsh "pgrep -f '$PAT/[b]oard-watch.py' >/dev/null" || { rsh "tail -20 '$R_WATCH/watch.log'" || true; die "the watcher didn't start (log above)"; }

say "4/4 What it sees right now"
rsh "python3 '$R_WATCH/board-watch.py' --check"

say "Done"
cat <<EOF
The watcher checks the board every ${WATCH_POLL}s and types into the $ENV_NAME Claude Code session once
Claude has been idle for ${WATCH_IDLE}s. Each question and answer is announced once.
  Status:   bash scripts/deploy-board-watch.sh --env $ENV_NAME --check
  Log:      $R_WATCH/watch.log (on the server)
  Remove:   bash scripts/deploy-board-watch.sh --env $ENV_NAME --remove
EOF
