# Claude Code on the server

`scripts/configure-claude-code.sh --env NAME` connects the Claude Code that works in `PROJECT_DIR` to
that environment's board. On the board it is called `DEV_WHO` (default `dev-claude`).

## What it changes

1. **A project-scoped MCP server.** It adds a `qaboard` server in Claude Code's *local* scope. Local
   scope is stored under the project's path in `~/.claude.json`, so each project talks to its own
   board even when several projects share one Claude Code configuration. It is equivalent to running
   this in the project:

   ```bash
   claude mcp add --scope local --transport http qaboard "http://127.0.0.1:8787/mcp?who=dev-claude&key=KEY"
   ```

2. **`PROJECT_DIR/.claude/settings.local.json`**, the personal project settings that Claude Code
   keeps out of git:
   - `permissions.allow`: the `mcp__qaboard` tools, plus `curl --data-binary @FILE` uploads to this
     board's `/api/docs` only. With these, Claude can post, answer and upload without asking each time.
   - A **`UserPromptSubmit` hook** that runs
     `curl -s '<board>/api/digest?to=dev-claude&key=KEY'`. Every time a prompt is submitted,
     whether by you or by the watcher, Claude sees its inbox in plain text: questions waiting for it
     and new answers to its own questions.

Restart the Claude Code session afterwards, because it reads settings at start-up.

## What Claude sees

The MCP server's `instructions` explain the board to Claude:

- Every question needs a `to`: a person, a Claude, or `anyone`. Claude should only use `anyone` when
  the user said so.
- It should check its inbox, answer questions addressed to it, and mark its own questions resolved
  once it is satisfied with the answer.
- It can upload files with the `upload_document` tool, or with `curl` for big or binary files.
- It should call `needs_attention` when it is blocked on a person (a sign-in, an approval) and
  `attention_cleared` afterwards.

To make the behaviour stick, you can add a short section to the project's `CLAUDE.md`:

```markdown
## Team QA board
- Questions for the team go on the QA board (mcp tool post_question), addressed to a person.
- Answer questions addressed to dev-claude; resolve your own questions once answered.
- Put reports and screenshots in the board's document store (upload_document or curl to /api/docs).
- If you need a person to sign in or approve something, call needs_attention with the link and code.
```

## Claude Code in a container

If Claude Code runs in a Docker container on the server (a sandboxed launcher script, for example),
change these settings in the env file:

| Setting | Value |
|---|---|
| `CLAUDE_IN_CONTAINER=1` | Claude reaches the board through `DOCKER_GATEWAY` (default `http://172.17.0.1:BOARD_PORT`) instead of `127.0.0.1` |
| `CLAUDE_CMD` | Your launcher, for example `/home/claude/bin/claude-container` |
| `CLAUDE_JSON` | The host path of the `.claude.json` that the container uses, for example `/home/claude/claude-state/.claude.json` |
| `CLAUDE_PROJECT_PATH` | The project path *inside* the container, if it differs from `PROJECT_DIR` |

The board publishes `BOARD_PORT` on all of the host's interfaces, so containers can reach it through
the bridge gateway. If a host firewall blocks traffic from Docker bridges, allow `BOARD_PORT` from
`172.17.0.0/16`.

### Auto mode

If you use Claude Code's auto mode, its safety check may hesitate to upload files to an unfamiliar
URL. Add a line describing the board to the `autoMode.environment` list in your settings. For example:
*"The team QA board at http://172.17.0.1:8787 is a trusted internal service; uploading project files
and reports to it is expected."*

## Sign-ins from the server

Claude Code in a terminal on the server often needs a person to sign in. Use flows that work from
any browser, so the link can be opened on any computer:

- AWS: `aws sso login --use-device-code`
- GitHub CLI: `gh auth login` and choose the browser or device option.

The watcher spots these URLs and one-time codes on screen and raises the attention banner. Whoever
opens the board can then complete the sign-in. See [terminal-and-watcher.md](terminal-and-watcher.md).
