#!/usr/bin/env bash
# deploy-web-terminal.sh - a browser terminal on the server that runs an environment's Claude Code
# (CLAUDE_CMD in PROJECT_DIR) inside a shared tmux session. Run from your computer, in the repo:
#
#     bash scripts/deploy-web-terminal.sh --env NAME
#     bash scripts/deploy-web-terminal.sh --env NAME --remove
#
# On the server (as the SSH account, no sudo):
#   - ttyd (a small web terminal; pinned version, checksum verified) in ~/.local/bin
#   - it listens on the server's 127.0.0.1:TERMINAL_PORT only: nothing new on the network, and
#     containers (including a containerised Claude) can't reach it. You reach it through the SSH tunnel.
#   - every browser tab attaches to ONE tmux session (socket qaboard-NAME, session "claude"): close
#     the tab and Claude keeps working; over SSH:  tmux -L qaboard-NAME attach
#   - a small supervisor, started from the account's crontab (@reboot), keeps it running
# The board watcher (deploy-board-watch.sh) types into this same tmux session.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

TTYD_VER="1.7.7"
TTYD_SHA="8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55"   # ttyd.x86_64, from the release's SHA256SUMS

parse_env_arg "$@"; set -- "${REST[@]+"${REST[@]}"}"
REMOVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --remove) REMOVE=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done
load_server; load_env "$ENV_ARG"; remote_dirs
TITLE="claude $ENV_NAME"
# pkill -f patterns with [x] so they never match the ssh command line that runs them
KILL_RUN="$ENV_NAME/terminal/[r]un.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if [ "$REMOVE" = 1 ]; then
  say "Removing the $ENV_NAME web terminal"
  rsh "crontab -l 2>/dev/null | grep -vF '$R_TERM/run.sh' | crontab -" || true
  rsh "pkill -f '$KILL_RUN'; pkill -f 'tty[d] .*-p $TERMINAL_PORT '; true"
  rsh "rm -rf '$R_TERM'" || true
  echo "Removed. (The tmux session, if any, keeps running: tmux -L $TMUX_SOCK kill-server)"
  exit 0
fi

# ----------------------------------------------------------------- 1. checks
say "1/4 Checking the server"
rsh true || die "cannot SSH to $SSH_TARGET"
rsh "test -d '$PROJECT_DIR'" || die "project folder $PROJECT_DIR doesn't exist on the server"
for c in tmux crontab flock python3; do
  rsh "command -v $c >/dev/null" || die "$c isn't installed on the server (an admin: sudo apt install tmux cron util-linux python3)"
done
CMD0="${CLAUDE_CMD%% *}"
rsh "cd '$PROJECT_DIR' && PATH=\"\$HOME/.local/bin:\$PATH\" command -v '$CMD0' >/dev/null" \
  || warn "'$CMD0' wasn't found on the server's PATH - the terminal will start, but CLAUDE_CMD may fail"
echo "OK. Project: $PROJECT_DIR   command: $CLAUDE_CMD"

# ----------------------------------------------------------------- 2. ttyd
say "2/4 Installing ttyd $TTYD_VER"
if rsh "test -x ~/.local/bin/ttyd && ~/.local/bin/ttyd --version 2>&1 | grep -q '$TTYD_VER'"; then
  echo "Already installed."
else
  [ "$(rsh 'uname -m')" = x86_64 ] || die "this script installs the x86_64 ttyd; install ttyd $TTYD_VER on the server as ~/.local/bin/ttyd by hand"
  curl -fsSL -o "$TMP/ttyd" "https://github.com/tsl0922/ttyd/releases/download/$TTYD_VER/ttyd.x86_64"
  echo "$TTYD_SHA  $TMP/ttyd" | sha256sum -c --quiet - || die "ttyd download failed its checksum - not installed"
  rsh "mkdir -p ~/.local/bin && cat > ~/.local/bin/ttyd.new && chmod +x ~/.local/bin/ttyd.new && mv ~/.local/bin/ttyd.new ~/.local/bin/ttyd" < "$TMP/ttyd"
  echo "Installed (checksum verified)."
fi

# ----------------------------------------------------------------- 3. scripts on the server
say "3/4 Writing the session scripts ($R_TERM)"
rsh "mkdir -p '$R_TERM'"

# Inside tmux: Claude Code in the project, then a shell so the window doesn't vanish.
rsh "cat > '$R_TERM/inner.sh' && chmod +x '$R_TERM/inner.sh'" <<EOF
#!/usr/bin/env bash
export PATH="\$HOME/.local/bin:\$PATH"
cd $(printf '%q' "$PROJECT_DIR") || exit 1
clear
echo "Claude Code in \$PWD  (this session keeps running if you close the browser tab)"
$CLAUDE_CMD
echo
echo "Claude Code has exited. Type  $CLAUDE_CMD  (add -c to continue the last conversation) to start it again."
exec bash -l
EOF

# Each browser tab: attach to the shared session, creating it if needed.
rsh "cat > '$R_TERM/session.sh' && chmod +x '$R_TERM/session.sh'" <<EOF
#!/usr/bin/env bash
export LANG=C.UTF-8 PATH="\$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
exec tmux -u -L $TMUX_SOCK -f '$R_TERM/tmux.conf' new-session -A -s $TMUX_SESSION '$R_TERM/inner.sh'
EOF

rsh "cat > '$R_TERM/tmux.conf'" <<EOF
set -g mouse on
set -g history-limit 50000
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",xterm-256color:Tc"
set -g status-style "bg=$TERMINAL_BAR_BG,fg=$TERMINAL_BAR_FG"
set -g status-left " $TITLE "
set -g status-left-length 30
set -g window-status-format ""
set -g window-status-current-format ""
set -g status-right " detach: close the tab | scroll: mouse wheel "
set -g status-right-length 60
EOF

# Supervisor: one copy at a time (flock), restarts ttyd if it stops, keeps its log small.
rsh "cat > '$R_TERM/run.sh' && chmod +x '$R_TERM/run.sh'" <<EOF
#!/usr/bin/env bash
cd '$R_TERM'
exec 9>'$R_TERM/.lock'
flock -n 9 || exit 0
while true; do
  [ -f ttyd.log ] && [ "\$(stat -c %s ttyd.log)" -gt 5000000 ] && mv -f ttyd.log ttyd.log.1
  "\$HOME/.local/bin/ttyd" -i lo -p $TERMINAL_PORT -W -O \\
    -t 'titleFixed=$TITLE' -t fontSize=15 -t disableLeaveAlert=true \\
    -t 'theme={"background":"$TERMINAL_BG","foreground":"$TERMINAL_FG","cursor":"$TERMINAL_CURSOR","selectionBackground":"$TERMINAL_BAR_BG"}' \\
    '$R_TERM/session.sh' >> ttyd.log 2>&1
  sleep 5
done
EOF

# ----------------------------------------------------------------- 4. start + survive reboots
say "4/4 Starting it, and registering it to start at boot"
rsh "(crontab -l 2>/dev/null | grep -vF '$R_TERM/run.sh'; echo '@reboot $R_TERM/run.sh') | crontab -"
rsh "pkill -f '$KILL_RUN'; pkill -f 'tty[d] .*-p $TERMINAL_PORT '; true"
sleep 1
rsh "nohup setsid '$R_TERM/run.sh' >/dev/null 2>&1 < /dev/null &"
ok=0
for _ in $(seq 1 10); do
  code=$(rsh "python3 -c \"import urllib.request as u; print(u.urlopen('http://127.0.0.1:$TERMINAL_PORT/', timeout=3).status)\" 2>/dev/null" || true)
  [ "$code" = 200 ] && { ok=1; break; }
  sleep 1
done
[ "$ok" = 1 ] || { rsh "tail -20 '$R_TERM/ttyd.log'" || true; die "ttyd didn't start on the server (log above)"; }

say "Done"
cat <<EOF
Running on the server at 127.0.0.1:$TERMINAL_PORT; starts again at boot.
Open it through the SSH tunnel (scripts/setup-tunnel.sh):  http://127.0.0.1:$TERMINAL_LOCAL_PORT
  - Every tab shows the same session. Closing the tab leaves Claude running.
  - From an SSH login on the server:  tmux -L $TMUX_SOCK attach
  - Remove:  bash scripts/deploy-web-terminal.sh --env $ENV_NAME --remove
EOF
