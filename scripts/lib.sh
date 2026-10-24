# lib.sh - shared helpers for the QA Board scripts. Sourced, not run.
# Reads config/server.env and, with load_env NAME, config/envs/NAME.env.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$REPO/config"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local r; read -r -p "$1 [y/N] " r; [[ "$r" =~ ^[Yy] ]]; }

# Run a command on the server. ClearAllForwardings: never start the tunnel's forwards from here.
rsh()  { ssh -i "$SSH_KEY" -o ClearAllForwardings=yes -o ServerAliveInterval=30 "$SSH_TARGET" "$@"; }

load_server() {
  [ -f "$CONFIG/server.env" ] || die "config/server.env is missing: cp config/server.env.example config/server.env and edit it"
  # shellcheck disable=SC1091
  . "$CONFIG/server.env"
  : "${SSH_TARGET:?set SSH_TARGET in config/server.env}" "${SSH_KEY:?set SSH_KEY in config/server.env}"
  : "${DOCKER_GATEWAY:=172.17.0.1}" "${DESKTOP_WHO:=me-claude}" "${LAN_HOST:=}" "${LAN_PREFIX:=}"
}

# remote_home: the server account's home folder (asked once per run)
remote_home() {
  [ -n "${_RHOME:-}" ] || _RHOME="$(rsh 'printf %s "$HOME"')" || die "cannot SSH to $SSH_TARGET"
  printf '%s' "$_RHOME"
}

list_envs() {  # the names of all configured environments
  local f; for f in "$CONFIG"/envs/*.env; do [ -f "$f" ] && basename "$f" .env; done
}

load_env() {   # load_env NAME - sets ENV_NAME, the env settings, and the derived remote paths
  ENV_NAME="$1"
  [[ "$ENV_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "environment names use a-z, 0-9 and - (got '$ENV_NAME')"
  local f="$CONFIG/envs/$ENV_NAME.env"
  [ -f "$f" ] || die "no $f (copy config/envs/example.env.example to it and edit). Configured: $(list_envs | xargs)"
  # defaults, then the file
  BOARD_NAME="$ENV_NAME"; BOARD_ACCENT="#4f46e5"; BOARD_PORT=8787; BOARD_LOCAL_PORT=18787; MAX_DOC_MB=50
  BRAND_DIR=""; PROJECT_DIR=""; CLAUDE_CMD="claude"; DEV_WHO="dev-claude"; CLAUDE_IN_CONTAINER=0
  CLAUDE_JSON=""; CLAUDE_PROJECT_PATH=""
  TERMINAL_PORT=7681; TERMINAL_LOCAL_PORT=17681
  TERMINAL_BG="#1b1530"; TERMINAL_FG="#ece8f5"; TERMINAL_BAR_BG="#2f2650"; TERMINAL_BAR_FG="#d6cdf0"; TERMINAL_CURSOR="#a99cf0"
  WATCH_POLL=5; WATCH_IDLE=20; WATCH_PROMPT_CHECK=1; WATCH_LIVE=1; EXTRA_FORWARDS=""
  # shellcheck disable=SC1090
  . "$f"
  : "${PROJECT_DIR:?set PROJECT_DIR in $f}"
  : "${CLAUDE_PROJECT_PATH:=$PROJECT_DIR}"
  KEY_FILE="$CONFIG/keys/$ENV_NAME.key"
  CNAME="qaboard-$ENV_NAME"          # board container
  TMUX_SOCK="qaboard-$ENV_NAME"      # tmux server of the web terminal
  TMUX_SESSION="claude"
  local p; for p in BOARD_PORT BOARD_LOCAL_PORT MAX_DOC_MB TERMINAL_PORT TERMINAL_LOCAL_PORT WATCH_POLL WATCH_IDLE; do
    [[ "${!p}" =~ ^[0-9]+$ ]] || die "$p must be a number in $f"
  done
  [[ "$BOARD_ACCENT" =~ ^#[0-9a-fA-F]{6}$ ]] || die "BOARD_ACCENT must look like #4f46e5"
}

remote_dirs() {  # R_BASE, R_BOARD, R_TERM, R_WATCH on the server (needs load_env first)
  R_BASE="${REMOTE_BASE:-$(remote_home)/qaboard}"
  R_BOARD="$R_BASE/$ENV_NAME/board"
  R_TERM="$R_BASE/$ENV_NAME/terminal"
  R_WATCH="$R_BASE/$ENV_NAME/watch"
}

board_key() {    # print the env's board key, making one the first time
  if [ ! -s "$KEY_FILE" ]; then
    mkdir -p "$CONFIG/keys"; chmod 700 "$CONFIG/keys"
    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$KEY_FILE")
    echo "Made a new board key for $ENV_NAME: $KEY_FILE" >&2
  fi
  local k; k="$(tr -d ' \n' < "$KEY_FILE")"
  [[ "$k" =~ ^[A-Za-z0-9._~-]+$ ]] || die "the key in $KEY_FILE may only use letters, digits and . _ ~ -"
  printf '%s' "$k"
}

board_url_for_claude() {  # how this env's Claude Code reaches its board
  if [ "$CLAUDE_IN_CONTAINER" = 1 ]; then echo "http://$DOCKER_GATEWAY:$BOARD_PORT"
  else echo "http://127.0.0.1:$BOARD_PORT"; fi
}

parse_env_arg() {  # parse_env_arg "$@" -> ENV_ARG and REST (remaining args)
  ENV_ARG=""; REST=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --env) ENV_ARG="${2:?--env needs a name}"; shift ;;
      *) REST+=("$1") ;;
    esac
    shift
  done
  if [ -z "$ENV_ARG" ]; then
    local envs; envs="$(list_envs)"
    [ "$(printf '%s\n' "$envs" | grep -c .)" = 1 ] && ENV_ARG="$envs" \
      || die "give --env NAME (configured: $(printf '%s ' $envs))"
  fi
}
