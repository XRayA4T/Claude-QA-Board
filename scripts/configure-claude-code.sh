#!/usr/bin/env bash
# configure-claude-code.sh - connect the Claude Code that works in an environment's project (on the
# server) to that environment's QA board. Run from your computer, in the repo:
#
#     bash scripts/configure-claude-code.sh --env NAME
#
# What it sets on the server (a .bak copy of every file it changes is kept; safe to re-run):
#   1. A "qaboard" MCP server for this project only (Claude Code's "local" scope: stored under the
#      project's path in CLAUDE_JSON), named DEV_WHO on the board. Each project gets its own board,
#      even when several projects share one Claude Code config.
#   2. <PROJECT_DIR>/.claude/settings.local.json (personal project settings, which Claude Code keeps
#      out of git, so the board key in the hook isn't committed):
#        - permissions: allow the qaboard tools, and curl uploads to this board's /api/docs
#        - a UserPromptSubmit hook that shows Claude its board inbox with every prompt
# Claude Code reads settings at start-up: restart that Claude Code session afterwards.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

parse_env_arg "$@"; set -- "${REST[@]+"${REST[@]}"}"
case "${1:-}" in -h|--help) sed -n '2,15p' "$0"; exit 0 ;; "") ;; *) die "unknown option $1" ;; esac
load_server; load_env "$ENV_ARG"
KEY="$(board_key)"
BOARD="$(board_url_for_claude)"
CJ="${CLAUDE_JSON:-$(remote_home)/.claude.json}"

say "Configuring Claude Code for $ENV_NAME"
echo "project $PROJECT_DIR (as Claude sees it: $CLAUDE_PROJECT_PATH), board $BOARD, config $CJ"
rsh "python3 - '$PROJECT_DIR' '$CLAUDE_PROJECT_PATH' '$CJ' '$BOARD' '$KEY' '$DEV_WHO'" <<'PY'
import json, os, shutil, sys, time
project, claude_path, cj_path, board, key, who = sys.argv[1:]
stamp = time.strftime("%Y%m%d-%H%M%S")

def load(path):
    return json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}

def save(path, data):
    if os.path.exists(path):
        shutil.copy2(path, "%s.bak.%s" % (path, stamp))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)

if not os.path.isdir(project):
    sys.exit("ERROR: %s doesn't exist on the server" % project)

# 1. project-scoped ("local") MCP server
cj = load(cj_path)
servers = cj.setdefault("projects", {}).setdefault(claude_path, {}).setdefault("mcpServers", {})
want = {"type": "http", "url": "%s/mcp?who=%s&key=%s" % (board, who, key)}
if servers.get("qaboard") != want:
    servers["qaboard"] = want
    save(cj_path, cj)
    print("1. qaboard MCP server for this project -> %s (as %s)" % (board, who))
else:
    print("1. qaboard MCP server already set")

# 2. project settings: permissions + inbox hook
sp = os.path.join(project, ".claude", "settings.local.json")
cfg = load(sp)
allow = cfg.setdefault("permissions", {}).setdefault("allow", [])
rules = ["mcp__qaboard"] + ["Bash(curl %s--data-binary @* %s%s/api/docs*)" % (f, q, board)
                            for f in ("", "-s ") for q in ("", "'", '"')]
added = [r for r in rules if r not in allow]
allow.extend(added)
hook_cmd = "curl -s --noproxy '*' --max-time 2 '%s/api/digest?to=%s&key=%s' || true" % (board, who, key)
hooks = cfg.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
for group in hooks:   # drop any older inbox hook (e.g. one pointing at another board)
    group["hooks"] = [h for h in group.get("hooks", []) if "/api/digest" not in h.get("command", "")]
hooks[:] = [g for g in hooks if g.get("hooks")]
hooks.append({"hooks": [{"type": "command", "command": hook_cmd}]})
save(sp, cfg)
print("2. %s: %d permission rule(s) added; inbox hook -> %s" % (sp, len(added), board))
PY
echo
echo "Done. Restart this project's Claude Code session to pick it up."
