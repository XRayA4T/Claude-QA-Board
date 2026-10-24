# AWS for Claude Code on the server

This guide sets up the AWS CLI so the Claude Code on the server can use AWS. Its sign-ins work
from any browser, so whoever sees the alert on the QA board can complete them.

The recommended setup is **IAM Identity Center (AWS SSO) with the device-code sign-in**:

- There are no long-lived keys on the server.
- Sessions expire on their own.
- The sign-in prints a link and a one-time code. The board watcher spots these and shows them in the
  red **Needs you** banner.

## 1. Install the AWS CLI v2 (on the server, as the server account)

No sudo is needed:

```bash
cd /tmp
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip   # aarch64: awscli-exe-linux-aarch64.zip
unzip -q awscliv2.zip
./aws/install -i ~/.local/aws-cli -b ~/.local/bin
aws --version
```

If Claude Code runs in a container, install the CLI in the container image instead. Then mount the
config folder so sign-ins survive restarts (see [Claude Code in a container](#claude-code-in-a-container)).

## 2. Create a profile

You need the details of your organisation's IAM Identity Center:

- the **start URL**, for example `https://my-org.awsapps.com/start`
- the **SSO region**
- the account and the permission set (role) that Claude should use

Use a role that has only what the project needs. A developer or read-only role is better than an
admin role.

```bash
aws configure sso --use-device-code
#   SSO session name:       my-org
#   SSO start URL:          https://my-org.awsapps.com/start
#   SSO region:             eu-west-1
#   SSO registration scopes: (Enter for the default)
#   -> open the link it prints, enter the code, approve; then pick the account and role
#   CLI default region:     eu-west-1
#   CLI profile name:       dev
```

This writes `~/.aws/config`:

```ini
[sso-session my-org]
sso_start_url = https://my-org.awsapps.com/start
sso_region = eu-west-1
sso_registration_scopes = sso:account:access

[profile dev]
sso_session = my-org
sso_account_id = 123456789012
sso_role_name = DeveloperAccess
region = eu-west-1
```

Make it the default for Claude Code by adding `export AWS_PROFILE=dev` to `~/.bashrc`. If you want it
for one project only, add it to the start of `CLAUDE_CMD` instead, for example
`CLAUDE_CMD="env AWS_PROFILE=dev claude"`.

## 3. Signing in (and again when the session expires)

```bash
aws sso login --use-device-code --profile dev
```

**Always use `--use-device-code`.** Without it, current CLI versions try to open a browser on the
server and wait for a redirect to `localhost`. That redirect can't reach a headless server. With the
flag, the CLI prints:

```
https://device.sso.eu-west-1.amazonaws.com/?user_code=ABCD-EFGH
Then enter the code:  ABCD-EFGH
```

The board watcher recognises this link and code and raises the attention banner. Anyone on the board
can then click **Open link**, check that the code matches, approve the sign-in and go back to work.
The banner clears itself when the CLI prints *Successfully logged into Start URL*, or when Claude
starts working again.

How long a session lasts is set in IAM Identity Center, typically 8–12 hours. Check it with
`aws sts get-caller-identity`.

## 4. Tell Claude how to behave

Add this to the project's `CLAUDE.md`:

```markdown
## AWS
- Use the AWS CLI with the profile in AWS_PROFILE (dev). Check access with `aws sts get-caller-identity`.
- If credentials have expired, run `aws sso login --use-device-code` and call the qaboard
  needs_attention tool with the link and code it prints; wait until the sign-in completes.
- Never create access keys, change IAM, or delete resources without asking on the QA board first.
```

Optional Claude Code permission rules in `.claude/settings.local.json`:

- Allow routine read-only commands without prompting.
- Always prompt before anything destructive.

```json
{
  "permissions": {
    "allow": [
      "Bash(aws sts get-caller-identity*)",
      "Bash(aws sso login --use-device-code*)",
      "Bash(aws s3 ls*)",
      "Bash(aws logs tail*)"
    ],
    "ask": [
      "Bash(aws * delete*)",
      "Bash(aws * terminate*)",
      "Bash(aws iam *)"
    ]
  }
}
```

The permission system is a convenience, not a security boundary. The real limit is the IAM role.

## Claude Code in a container

- Install the AWS CLI in the image.
- Mount a persistent folder as the container's `~/.aws`, for example
  `-v /home/claude/claude-state/aws:/home/claude/.aws`. The profile and the SSO token cache then
  survive restarts.
- Pass `-e AWS_PROFILE=dev`.
- The device-code sign-in works the same way inside the container. Only the person approving it
  needs a browser.

## Local development against AWS services

If the project runs local stand-ins, such as DynamoDB Local or LocalStack, give them their own profile
with dummy keys and an `endpoint_url`. This keeps the real SSO profile from being used by accident:

```ini
[profile local]
region = eu-west-1
aws_access_key_id = local
aws_secret_access_key = local
endpoint_url = http://localhost:8000
```

To browse them from your computer, add the ports to `EXTRA_FORWARDS`. Bind them to the server's
loopback, not `0.0.0.0`, so they aren't exposed on the LAN.

## Alternatives

| Option | When to use it |
|---|---|
| **EC2 instance role** | The server is an EC2 instance. There are no sign-ins at all; credentials come from the instance metadata. |
| **IAM Roles Anywhere** | Long-running servers outside AWS that you can give an X.509 certificate. |
| **Static access keys** (`aws configure`) | Avoid. If you must, use a dedicated IAM user with minimal rights, and rotate the keys. |
