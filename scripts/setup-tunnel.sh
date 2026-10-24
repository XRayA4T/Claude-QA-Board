#!/usr/bin/env bash
# setup-tunnel.sh - reach every configured environment on the server through one always-on SSH
# tunnel, and connect Claude Desktop to each environment's QA board. Run on your computer (each
# teammate runs it on theirs, with their own SSH login in config/server.env):
#
#     bash scripts/setup-tunnel.sh            set up / repair (safe to re-run)
#     bash scripts/setup-tunnel.sh --no-desktop   only the tunnel
#     bash scripts/setup-tunnel.sh --remove   undo (tunnel service + ssh config block)
#
# For every config/envs/NAME.env it forwards, on 127.0.0.1 of this computer:
#   BOARD_LOCAL_PORT     -> the board          TERMINAL_LOCAL_PORT -> the web terminal
#   EXTRA_FORWARDS       -> e.g. the app you are developing
# - adds a managed "Host qaboard" block to ~/.ssh/config (backup kept); with LAN_HOST/LAN_PREFIX set
#   it connects straight to the LAN address when this computer is on that network
# - Linux: a systemd user service "qaboard-tunnel" keeps it up and reconnects after sleep / network
#   changes. Elsewhere it tells you the command to run (ssh -N qaboard).
# - Claude Desktop: one MCP server per environment, "qaboard-NAME", as DESKTOP_WHO, through
#   `python3 server/qaboard.py bridge URL` (no Node needed). The board keys come from config/keys/.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

ALIAS="qaboard"
SSH_CFG="$HOME/.ssh/config"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/qaboard-tunnel.service"
BEGIN="# >>> qaboard tunnel (managed by setup-tunnel.sh) >>>"
END="# <<< qaboard tunnel <<<"
case "$(uname -s)" in
  Darwin) DESKTOP_CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
  *)      DESKTOP_CFG="$HOME/.config/Claude/claude_desktop_config.json" ;;
esac

REMOVE=0; DESKTOP=1
while [ $# -gt 0 ]; do
  case "$1" in
    --remove) REMOVE=1 ;;
    --no-desktop) DESKTOP=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done

remove_block() {  # strip our managed block from ~/.ssh/config
  [ -f "$SSH_CFG" ] || return 0
  python3 - "$SSH_CFG" "$BEGIN" "$END" <<'PY'
import sys
path, begin, end = sys.argv[1:]
out, skip = [], False
for line in open(path):
    if line.rstrip("\n") == begin: skip = True; continue
    if line.rstrip("\n") == end: skip = False; continue
    if not skip: out.append(line)
text = "".join(out).strip("\n")
open(path, "w").write(text + "\n" if text else "")
PY
}
HAVE_SYSTEMD=0; command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1 && HAVE_SYSTEMD=1

if [ "$REMOVE" = 1 ]; then
  say "Removing the tunnel"
  if [ "$HAVE_SYSTEMD" = 1 ]; then
    systemctl --user disable --now qaboard-tunnel 2>/dev/null || true
    rm -f "$UNIT"; systemctl --user daemon-reload
  fi
  remove_block
  echo "Removed. Claude Desktop's config still has the qaboard-* entries - remove them there if needed."
  exit 0
fi

load_server
[ -f "$SSH_KEY" ] || die "SSH key not found: $SSH_KEY"
ENVS="$(list_envs)"; [ -n "$ENVS" ] || die "no environments in config/envs/ yet"
SSH_USER="${SSH_TARGET%@*}"; SSH_HOST="${SSH_TARGET#*@}"
[ "$SSH_USER" != "$SSH_TARGET" ] || SSH_USER="$USER"

# Collect the forwards of every environment: "local remote description"
FORWARDS=()
for e in $ENVS; do
  load_env "$e"
  FORWARDS+=("$BOARD_LOCAL_PORT $BOARD_PORT $e-board" "$TERMINAL_LOCAL_PORT $TERMINAL_PORT $e-terminal")
  for x in $EXTRA_FORWARDS; do
    IFS=: read -r lp rp what <<<"$x"
    [[ "$lp" =~ ^[0-9]+$ && "$rp" =~ ^[0-9]+$ ]] || die "bad EXTRA_FORWARDS entry '$x' in envs/$e.env (local:remote:description)"
    FORWARDS+=("$lp $rp $e-${what:-port$rp}")
  done
done
dupes="$(printf '%s\n' "${FORWARDS[@]}" | awk '{print $1}' | sort | uniq -d | xargs)"
[ -z "$dupes" ] || die "local port(s) used twice across the environments: $dupes"

# ----------------------------------------------------------------- 1. ssh config
say "1/4 SSH config ($SSH_CFG)"
mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; touch "$SSH_CFG"; chmod 600 "$SSH_CFG"
cp "$SSH_CFG" "$SSH_CFG.bak.$(date +%Y%m%d-%H%M%S)"
remove_block
# Our block goes at the TOP: ssh uses the first value it finds, so the LAN HostName has to come
# before the default one (and before any other matching Host entries).
{
  echo "$BEGIN"
  if [ -n "$LAN_HOST" ] && [ -n "$LAN_PREFIX" ]; then
    echo "# On the server's LAN: connect straight to its LAN address"
    if [ "$(uname -s)" = Darwin ]; then
      echo "Match host $ALIAS exec \"ifconfig | grep -Eq 'inet $LAN_PREFIX'\""
    else
      echo "Match host $ALIAS exec \"ip -4 addr | grep -Eq 'inet $LAN_PREFIX'\""
    fi
    echo "    HostName $LAN_HOST"
    echo
  fi
  echo "Host $ALIAS"
  echo "    HostName $SSH_HOST"
  echo "    User $SSH_USER"
  echo "    IdentityFile $SSH_KEY"
  echo "    IdentitiesOnly yes"
  for f in "${FORWARDS[@]}"; do
    read -r lp rp what <<<"$f"
    echo "    # $what"
    echo "    LocalForward 127.0.0.1:$lp localhost:$rp"
  done
  echo "    ExitOnForwardFailure yes"
  echo "    ServerAliveInterval 10"
  echo "    ServerAliveCountMax 3"
  echo "    ConnectTimeout 10"
  echo "$END"
  echo
  cat "$SSH_CFG"
} > "$SSH_CFG.tmp" && mv "$SSH_CFG.tmp" "$SSH_CFG"
chmod 600 "$SSH_CFG"
echo "'Host $ALIAS' has ${#FORWARDS[@]} forwards (connects to $(ssh -G "$ALIAS" | awk '/^hostname /{print $2}') from here)."

# ----------------------------------------------------------------- 2. first connection
say "2/4 Testing SSH (accepts the server's host key the first time)"
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ClearAllForwardings=yes "$ALIAS" true \
  || die "ssh $ALIAS failed. If the key has a passphrase, load it into ssh-agent first; try: ssh $ALIAS"
echo "OK."

# ----------------------------------------------------------------- 3. keep it running
say "3/4 Tunnel service"
board_ok() {  # board_ok PORT KEY
  python3 -c "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:$1/health?key=$2', timeout=3)" 2>/dev/null
}
if [ "$HAVE_SYSTEMD" = 1 ]; then
  if ! systemctl --user is-active --quiet qaboard-tunnel 2>/dev/null; then
    for f in "${FORWARDS[@]}"; do
      read -r lp _ what <<<"$f"
      if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$lp "; then
        die "port $lp ($what) is already in use on this computer; the tunnel would refuse to start"
      fi
    done
  fi
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=SSH tunnel to the QA Board development server
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -o BatchMode=yes $ALIAS
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable qaboard-tunnel >/dev/null 2>&1
  systemctl --user restart qaboard-tunnel
  sleep 2
  for e in $ENVS; do
    load_env "$e"
    if [ ! -s "$KEY_FILE" ]; then warn "$e: no key in $KEY_FILE (get it from whoever deployed the board)"; continue; fi
    k="$(tr -d ' \n' < "$KEY_FILE")"; ok=0
    for _ in $(seq 1 10); do board_ok "$BOARD_LOCAL_PORT" "$k" && { ok=1; break; }; sleep 1; done
    if [ "$ok" = 1 ]; then echo "$e board reachable through the tunnel."
    else warn "$e board didn't answer on 127.0.0.1:$BOARD_LOCAL_PORT (not deployed yet, or wrong key?)"; fi
  done
  loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'yes' \
    || echo "(The tunnel runs while you're logged in. To keep it up when logged out: sudo loginctl enable-linger $USER)"
else
  echo "No systemd user session here. Keep this running in a terminal (or with autossh / a launch agent):"
  echo "    ssh -N $ALIAS"
fi

# ----------------------------------------------------------------- 4. Claude Desktop
say "4/4 Claude Desktop"
if [ "$DESKTOP" = 0 ]; then
  echo "Skipped (--no-desktop)."
elif [ ! -f "$DESKTOP_CFG" ]; then
  warn "$DESKTOP_CFG not found (is Claude Desktop installed? open it once first) - skipped."
elif ask "Is Claude Desktop FULLY quit (it rewrites its config while running)?"; then
  cp "$DESKTOP_CFG" "$DESKTOP_CFG.bak.$(date +%Y%m%d-%H%M%S)"
  ENTRIES=()
  for e in $ENVS; do
    load_env "$e"
    [ -s "$KEY_FILE" ] || { warn "$e: no key in $KEY_FILE - not added"; continue; }
    ENTRIES+=("qaboard-$e" "http://127.0.0.1:$BOARD_LOCAL_PORT/mcp?who=$DESKTOP_WHO&key=$(tr -d ' \n' < "$KEY_FILE")")
  done
  python3 - "$DESKTOP_CFG" "$REPO/server/qaboard.py" "${ENTRIES[@]}" <<'PY'
import json, sys
path, script, *pairs = sys.argv[1:]
d = json.load(open(path))
servers = d.setdefault("mcpServers", {})
for name, url in zip(pairs[::2], pairs[1::2]):
    servers[name] = {"command": "python3", "args": [script, "bridge", url]}
    print("%-20s -> %s" % (name, url.split("/mcp")[0]))
json.dump(d, open(path, "w"), indent=2)
PY
  echo "Reopen Claude Desktop to use them, then ask e.g. \"check my QA board inbox\"."
else
  echo "Skipped. Quit Claude Desktop and re-run this script."
fi

say "Done"
echo "On this computer:"
for f in "${FORWARDS[@]}"; do
  read -r lp _ what <<<"$f"
  printf '  %-28s http://127.0.0.1:%s\n' "$what" "$lp"
done
cat <<EOF
Board pages need ?key=... (the keys are in config/keys/).
Tunnel status:  systemctl --user status qaboard-tunnel     Undo: bash scripts/setup-tunnel.sh --remove
EOF
