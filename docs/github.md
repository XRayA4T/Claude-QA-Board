# GitHub for Claude Code on the server

This guide sets up the GitHub CLI (`gh`) and git so the Claude Code on the server can do the
following:

- clone, pull and push
- open pull requests
- read issues and CI runs

The sign-in prints a link and a code that the board watcher shows in the **Needs you** banner, so
anyone on the board can complete it.

## 1. Decide which identity Claude uses

| Identity | Pros and cons |
|---|---|
| **A dedicated machine account** (e.g. `acme-dev-claude`) added to the repos it needs | **Recommended.** Its commits, PRs and comments are clearly Claude's. Its access can be limited and revoked on its own. |
| **A fine-grained personal access token** from a team member's account | Quick to set up and limited to chosen repos and permissions, but the actions appear as that person. |
| **Your own full `gh` login** | Avoid: Claude gets everything you can do, on every repo. |

Whichever you choose, protect `main` with branch protection or rulesets, so Claude works through
pull requests.

## 2. Install gh (on the server)

Without sudo:

```bash
V=2.62.0   # pick the current release from https://github.com/cli/cli/releases
curl -fsSL "https://github.com/cli/cli/releases/download/v$V/gh_${V}_linux_amd64.tar.gz" | tar xz -C /tmp
install -D /tmp/gh_${V}_linux_amd64/bin/gh ~/.local/bin/gh
gh --version
```

With sudo, use the [official apt repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md).
If Claude Code runs in a container, install `gh` in the image.

## 3. Sign in

### Option A: device sign-in (the browser can be on any computer)

```bash
gh auth login --hostname github.com --git-protocol https --web
```

On a server, `gh` prints a one-time code and the device page:

```
! First copy your one-time code: 1A2B-3C4D
Press Enter to open https://github.com/login/device in your browser...
```

Press Enter. There is no browser on the server, so nothing opens, but `gh` keeps waiting. The board
watcher shows the link and code in the attention banner. Whoever approves it must be **signed in to
GitHub as the identity Claude should use**, such as the machine account; a private window helps.
`gh` then prints *Logged in as …* and the banner clears.

### Option B: a token

Create a **fine-grained token** under *Settings → Developer settings → Personal access tokens →
Fine-grained tokens*. Use:

- **Repository access:** only the repos Claude works on
- **Repository permissions:** Contents (read and write), Pull requests (read and write),
  Issues (read and write) if needed, Actions (read) to see CI, Metadata (read, automatic)

Then, on the server:

```bash
gh auth login --hostname github.com --with-token < token.txt && shred -u token.txt
```

Tokens expire on the date you set. When one expires, repeat this step with a new token.

### Make git use the gh login

```bash
gh auth setup-git                      # git push/pull over https now uses gh's credentials
git config --global user.name  "dev-claude"
git config --global user.email "123456+acme-dev-claude@users.noreply.github.com"   # the account's no-reply address
gh auth status
```

The no-reply address is shown under *Settings → Emails* of that account. Using it keeps a real
email address out of commit history.

## 4. Tell Claude how to behave

Add this to the project's `CLAUDE.md`:

```markdown
## GitHub
- Work on a branch and open a pull request with `gh pr create`; never push to main.
- Use `gh pr view`, `gh pr checks` and `gh run view --log-failed` to follow CI.
- If gh reports it isn't logged in, run `gh auth login --hostname github.com --git-protocol https --web`,
  press Enter at the prompt, and call the qaboard needs_attention tool with the link and code.
- Ask on the QA board before force-pushing, deleting branches, or changing repository settings.
```

Optional Claude Code permission rules in `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(gh pr view*)", "Bash(gh pr list*)", "Bash(gh pr checks*)", "Bash(gh pr diff*)",
      "Bash(gh issue view*)", "Bash(gh issue list*)", "Bash(gh run view*)", "Bash(gh run list*)",
      "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git fetch*)"
    ],
    "ask": [
      "Bash(git push*)", "Bash(gh pr merge*)", "Bash(gh repo *)", "Bash(gh api *)"
    ],
    "deny": [
      "Bash(git push --force*)", "Bash(git push -f*)"
    ]
  }
}
```

## Claude Code in a container

- Mount a persistent folder as the container's `~/.config/gh` (and `~/.gitconfig`), for example
  `-v /home/claude/claude-state/gh:/home/claude/.config/gh`, so the login survives restarts.
- Or pass a token in the environment: `-e GH_TOKEN=...`. `gh` and `gh`-configured git use it
  directly. Keep it in a file readable only by the server account, not on the command line, for
  example `--env-file`.

## More than one account (e.g. on your own computer)

`gh` can hold several logins:

```bash
gh auth login --web                        # add another account
gh auth status                             # list them; one is active
gh auth switch --user other-account        # change the active one (git follows, after gh auth setup-git)
```

To push to one repository as a different account without switching, use a token for that push:
`git -c credential.helper= push https://OTHER@github.com/owner/repo.git main`, and enter the token
as the password.

## SSH instead of HTTPS

If you prefer SSH, give the machine account (or a single repo, as a **deploy key** with write access)
an SSH key from the server, and use `git@github.com:owner/repo.git` remotes. `gh` still needs its own
login, or `GH_TOKEN`, for API commands such as pull requests and CI.
