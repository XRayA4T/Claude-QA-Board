#!/usr/bin/env bash
# deploy-board.sh - run an environment's QA board in a small container on the server, restarting
# automatically after crashes and reboots. Run from your computer, in the repo:
#
#     bash scripts/deploy-board.sh --env NAME [--import-db FILE] [--terminal-url URL]
#
#   --import-db FILE   copy an existing qaboard.db to the server (only if the server has none yet)
#   --terminal-url URL where browsers reach this environment's web terminal, for the board's Live tab
#                      (default: the SSH tunnel, http://127.0.0.1:TERMINAL_LOCAL_PORT)
#
# Settings come from config/server.env and config/envs/NAME.env. The board key is
# config/keys/NAME.key (made on the first run). Safe to re-run: updates qaboard.py and the look,
# keeps the board's data.
#
# Server layout:  <REMOTE_BASE>/NAME/board/qaboard.py   (mounted read-only into the container)
#                 <REMOTE_BASE>/NAME/board/data/        (qaboard.db, docs/, brand/)
# Container:      qaboard-NAME, image python:3.12-alpine, read-only root, --restart unless-stopped,
#                 memory cap sized to MAX_DOC_MB.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

parse_env_arg "$@"; set -- "${REST[@]+"${REST[@]}"}"
IMPORT_DB=""; TERM_URL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --import-db) IMPORT_DB="${2:?}"; shift ;;
    --terminal-url) TERM_URL="${2:?}"; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done
load_server; load_env "$ENV_ARG"
KEY="$(board_key)"
: "${TERM_URL:=http://127.0.0.1:$TERMINAL_LOCAL_PORT}"
IMAGE="python:3.12-alpine"
[[ "$BOARD_NAME" =~ ^[A-Za-z0-9\ \&.,\'-]+$ ]] || die "BOARD_NAME may only use letters, digits, spaces and & . , ' -"
[[ "$TERM_URL" =~ ^https?://[A-Za-z0-9.:/_-]+$ ]] || die "bad terminal URL: $TERM_URL"
SRC="$REPO/server/qaboard.py"
python3 -m py_compile "$SRC" || die "server/qaboard.py has a syntax error"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

say "Checking the server"
rsh 'docker version --format "{{.Server.Version}}"' >/dev/null 2>&1 || die "Docker isn't usable by $SSH_TARGET (is it in the docker group?)"
remote_dirs
rsh "mkdir -p '$R_BOARD/data'"
echo "OK. Board folder: $R_BOARD"

say "Copying qaboard.py"
rsh "cat > '$R_BOARD/qaboard.py.new' && mv '$R_BOARD/qaboard.py.new' '$R_BOARD/qaboard.py'" < "$SRC"

if [ -n "$BRAND_DIR" ]; then
  case "$BRAND_DIR" in /*) B="$BRAND_DIR" ;; *) B="$REPO/$BRAND_DIR" ;; esac
  [ -f "$B/brand.css" ] || die "BRAND_DIR $B has no brand.css"
  say "Copying the board's look ($BRAND_DIR)"
  tar czf - -C "$B" . | rsh "rm -rf '$R_BOARD/data/brand.new' && mkdir -p '$R_BOARD/data/brand.new' && tar xzf - -C '$R_BOARD/data/brand.new' && rm -rf '$R_BOARD/data/brand' && mv '$R_BOARD/data/brand.new' '$R_BOARD/data/brand'"
else
  rsh "rm -rf '$R_BOARD/data/brand'"
fi

if [ -n "$IMPORT_DB" ]; then
  [ -f "$IMPORT_DB" ] || die "no such file: $IMPORT_DB"
  if rsh "test -s '$R_BOARD/data/qaboard.db'"; then
    warn "the server already has a qaboard.db - kept it, $IMPORT_DB not sent"
  else
    say "Copying $IMPORT_DB (a consistent snapshot)"
    python3 - "$IMPORT_DB" "$TMP/qaboard.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
    rsh "cat > '$R_BOARD/data/qaboard.db'" < "$TMP/qaboard.db"
  fi
fi

say "Starting $CNAME ($BOARD_NAME, port $BOARD_PORT, documents up to $MAX_DOC_MB MB)"
NAME_Q=$(printf '%q' "$BOARD_NAME")
# A file Claude sends through upload_document arrives base64-encoded inside JSON and is decoded in
# memory, which takes several times its size: give the board room for the largest allowed file.
if [ "$MAX_DOC_MB" -le 20 ]; then MEM=256m; elif [ "$MAX_DOC_MB" -le 60 ]; then MEM=512m; else MEM=1g; fi
rsh bash -s <<EOF
set -euo pipefail
docker pull -q $IMAGE >/dev/null
docker rm -f $CNAME >/dev/null 2>&1 || true
docker run -d --name $CNAME \\
  --restart unless-stopped \\
  --user "\$(id -u):\$(id -g)" \\
  --memory $MEM --cpus 0.5 --pids-limit 128 \\
  --read-only --tmpfs /tmp \\
  -p $BOARD_PORT:8787 \\
  -v '$R_BOARD/qaboard.py':/app/qaboard.py:ro \\
  -v '$R_BOARD/data':/data \\
  --health-cmd "python3 -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:8787/health?key=$KEY', timeout=3)\"" \\
  --health-interval 30s --health-retries 3 \\
  --log-opt max-size=5m --log-opt max-file=2 \\
  $IMAGE python3 -u /app/qaboard.py serve --port 8787 --db /data/qaboard.db --max-doc-mb $MAX_DOC_MB \\
    --name $NAME_Q --accent '$BOARD_ACCENT' --terminal-url '$TERM_URL' --key '$KEY' >/dev/null
EOF

say "Checking it's up"
OK=0
for _ in $(seq 1 10); do
  if rsh "docker exec $CNAME python3 -c \"import urllib.request as u; print(u.urlopen('http://127.0.0.1:8787/health?key=$KEY', timeout=3).read().decode())\"" 2>/dev/null; then
    OK=1; break
  fi
  sleep 1
done
[ "$OK" = 1 ] || { rsh "docker logs --tail 30 $CNAME" || true; die "the board didn't come up (log above)"; }

if ! rsh "systemctl is-enabled docker" 2>/dev/null | grep -qx enabled; then
  warn "Docker isn't set to start at boot. An admin needs to run once on the server: sudo systemctl enable docker"
fi

say "Done"
cat <<EOF
$BOARD_NAME board
  through the tunnel (scripts/setup-tunnel.sh):  http://127.0.0.1:$BOARD_LOCAL_PORT/?key=$KEY
  on the server itself:                          http://127.0.0.1:$BOARD_PORT/?key=$KEY
  for this environment's Claude Code:            $(board_url_for_claude)/mcp?who=$DEV_WHO&key=...
Manage it on the server:  docker logs -f $CNAME | docker restart $CNAME | docker rm -f $CNAME
Back up:                  $R_BOARD/data/ (qaboard.db and docs/)
EOF
echo "Note: the board listens on port $BOARD_PORT on all of the server's interfaces (protected by the key)."
echo "      Don't forward that port from the internet; use the SSH tunnel instead."
