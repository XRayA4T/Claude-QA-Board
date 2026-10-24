# Web terminal, watcher and live view

## The web terminal

`scripts/deploy-web-terminal.sh --env NAME` runs [ttyd](https://github.com/tsl0922/ttyd) on the server.

- ttyd listens on the server's **loopback only** (`-i lo`) on `TERMINAL_PORT`. You reach it through
  the SSH tunnel at `http://127.0.0.1:TERMINAL_LOCAL_PORT`. Containers can't reach it, so a
  containerised Claude can't type into its own terminal.
- Every browser tab attaches to the same tmux session (`tmux -L qaboard-NAME`, session `claude`).
  That session runs `CLAUDE_CMD` in `PROJECT_DIR`. Closing the tab leaves Claude running.
- A supervisor (`run.sh`, with one copy at a time enforced by `flock`) restarts ttyd if it stops.
  It is started from the account's crontab `@reboot`, so it comes back after a reboot.
- The terminal's colours come from `TERMINAL_*` in the env file. Give each environment a different
  colour so you can tell them apart.

On the board's **Live** tab, **Open terminal** shows the terminal in a floating window over the
board. You can drag, resize and maximise it. Esc is passed through to Claude, so it doesn't close
the window. The window loads the board's `--terminal-url`, which by default is the tunnel address.
Each viewer can change it with the link next to the button.

![The web terminal in its floating window](images/terminal.png)

## The watcher

`watcher/board-watch.py` runs on the server, one per environment, installed by
`scripts/deploy-board-watch.sh --env NAME`. It is plain Python and uses **no tokens**. Every
`WATCH_POLL` seconds it does the following.

### 1. Wakes Claude when there is something for it

It asks the board for:

- open questions addressed to `DEV_WHO` (or to `anyone`) that it didn't write
- new replies from others on threads it is part of: addressed to it, started by it, or answered by it

When something is new, it waits until the Claude session is **idle**:

- the prompt area hasn't changed for `WATCH_IDLE` seconds, so you are not halfway through typing
- Claude isn't working (no "esc to interrupt")
- there is no draft in the input box. Grey suggestion text is ignored.
- with `WATCH_PROMPT_CHECK=1`, no permission or question prompt is on screen, where Enter would answer it

Then it types one short line into the session with `tmux send-keys`, for example:

```
[QA board] New for you: question #12 from alice ("Why does signup reject +?"). Use the qaboard tools to read it and answer or act on it, then carry on with what you were doing.
```

The inbox hook also shows Claude the details. Each question or answer is announced only once; the
announced items are kept in `state.json`.

### 2. Streams the console (`WATCH_LIVE=1`)

Every 2 s it captures the tmux pane, with colours, and posts it to `/api/live`. The board shows it:

- on the **Live** tab, as a read-only console with ANSI colours that keeps its scroll position
- in the **mini console** in the header. Its LED flashes green for a quarter of a second whenever the
  console changes, so you can see Claude working from any tab.

![Live view](images/live.png)

![Mini console](images/mini-console.png)

> Everyone with the board key can see whatever the console shows. Leave `WATCH_LIVE=0` if that
> includes anything sensitive.

### 3. Raises "needs attention"

The board has one attention slot per environment. It can be set by:

- **Claude**, with the `needs_attention` MCP tool (a reason, plus an optional URL and code), and cleared
  with `attention_cleared`
- **The watcher**, when it sees a device sign-in on screen: an AWS SSO device page or GitHub's
  `/login/device`, together with a one-time code (`ABCD-EFGH`). Both the URL and the code have to be
  there, so a URL mentioned in a file or in `CLAUDE.md` doesn't trigger a false alarm.
- **A person**, through `POST /api/attention`

While it is set, the Live tab shows a red banner with **Open link**, the **code** with a **Copy**
button, and **Mark as handled**. The mini console's border pulses red on every tab. The watcher
clears the alert when it sees a sign-in success message, or when Claude goes from idle back to
working. If you dismiss an alert, the watcher remembers that and doesn't raise the same one again.

![Attention banner](images/attention.png)

### Config

`<REMOTE_BASE>/NAME/watch/config.json` is written by the deploy script with mode 600, because it
contains the key:

```json
{"board": "http://127.0.0.1:8787", "key": "...", "who": "dev-claude",
 "tmux_socket": "qaboard-NAME", "tmux_session": "claude", "poll": 5, "idle": 20,
 "prompt_check": true, "live": true, "live_interval": 2, "title": "claude NAME"}
```

Log: `<REMOTE_BASE>/NAME/watch/watch.log`. Check: `bash scripts/deploy-board-watch.sh --env NAME --check`.
