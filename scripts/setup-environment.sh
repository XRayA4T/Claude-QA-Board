#!/usr/bin/env bash
# setup-environment.sh - set up a whole environment on the server in one go. Run from your computer,
# in the repo, after filling in config/server.env and config/envs/NAME.env:
#
#     bash scripts/setup-environment.sh --env NAME
#
# Steps (each is its own script and can be re-run on its own):
#   1. the QA board                         deploy-board.sh --env NAME
#   2. Claude Code on the server -> board   configure-claude-code.sh --env NAME
#   3. the web terminal                     deploy-web-terminal.sh --env NAME
#   4. the board watcher                    deploy-board-watch.sh --env NAME
#   5. this computer's tunnel + Claude Desktop (all environments)   setup-tunnel.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"
parse_env_arg "$@"
case "${REST[0]:-}" in -h|--help) sed -n '2,13p' "$0"; exit 0 ;; "") ;; *) die "unknown option ${REST[0]}" ;; esac

step() { printf '\n\033[1;36m######## %s\033[0m\n' "$*"; }
step "1/5 QA board";               bash "$HERE/deploy-board.sh" --env "$ENV_ARG"
step "2/5 Claude Code -> board";   bash "$HERE/configure-claude-code.sh" --env "$ENV_ARG"
step "3/5 Web terminal";           bash "$HERE/deploy-web-terminal.sh" --env "$ENV_ARG"
step "4/5 Board watcher";          bash "$HERE/deploy-board-watch.sh" --env "$ENV_ARG"
step "5/5 Tunnel + Claude Desktop"; bash "$HERE/setup-tunnel.sh"

load_server; load_env "$ENV_ARG"
step "All done"
cat <<EOF
$BOARD_NAME
  board:     http://127.0.0.1:$BOARD_LOCAL_PORT/?key=$(board_key)
  terminal:  http://127.0.0.1:$TERMINAL_LOCAL_PORT
Share the board link (with its key) only with your team.
EOF
