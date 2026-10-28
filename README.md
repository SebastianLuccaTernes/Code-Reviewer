# Self-hosted PR Reviewer

When a pull request is opened on GitHub, Claude reviews it and posts the result as a PR comment. It runs in its own container and uses no GitHub Actions minutes.

A review starts when:

- a non-draft PR is opened or marked ready for review (each commit is reviewed automatically at most once), or
- an OWNER, MEMBER or COLLABORATOR comments `@claude review` on a PR.

The service verifies the webhook signature, answers right away, then in the background fetches the PR and its diff, checks out the head commit into a temporary directory, runs `claude -p` there with read-only tools, posts the result as a comment and deletes the directory.

## Setup with an agent

An agent with shell access on the server, for example Claude Code, can do the whole [Setup](#setup) on its own. You only provide what it can't create itself:

- `GH_TOKEN` and the Claude credential, as described in [Tokens](#1-tokens).
- `WEBHOOK_TOKEN`: a second fine-grained token for the same repos with **Webhooks: Read and write** (classic token: `admin:repo_hook`). The agent uses it once to add the webhooks and doesn't store it.
- `DOMAIN` (optional): a domain whose DNS record points to the server. Without one, the agent sets up a Cloudflare quick tunnel. Its URL changes whenever the tunnel restarts, so it's only good for testing.

Fill in the values at the top of the prompt and give it to the agent. Start Claude Code with `claude --dangerously-skip-permissions` so it doesn't ask before every command. Only do that on a server you own. The values you paste stay in the agent's session history on the server.

```text
Set up the self-hosted PR reviewer from https://github.com/SebastianLuccaTernes/Code-Reviewer on this server. Don't ask me anything: fix problems yourself and stop only if a step can't be done. Never print secret values.

REPOS=owner/repo,owner/other-repo
GH_TOKEN=
WEBHOOK_TOKEN=
CLAUDE_CODE_OAUTH_TOKEN=
ANTHROPIC_API_KEY=
DOMAIN=

Exactly one of the two Claude values is filled in. WEBHOOK_TOKEN is only for steps 6 and 7; never write it to disk. DOMAIN may be empty.

1. Install what's missing: git, curl, openssl, and Docker with the compose plugin (https://get.docker.com). Use sudo if you aren't root.
2. Clone the repo to /opt/code-reviewer, or git pull it if it's already there, and work in that directory.
3. Create .env from .env.example. Set GH_TOKEN, the Claude value I filled in, ALLOWED_REPOS=$REPOS and GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32). If .env already exists, keep its GITHUB_WEBHOOK_SECRET. Then run chmod 600 .env.
4. Run docker compose up -d --build and wait until curl -fsS http://localhost:8000/health returns {"ok":true}. If it doesn't, read docker compose logs.
5. Make the service reachable over HTTPS and set HOST:
   - DOMAIN is set: install Caddy from its official package repo. Give it a Caddyfile site block for $DOMAIN with reverse_proxy 127.0.0.1:8000 and reload it. If a local firewall is active, allow ports 80 and 443. HOST=$DOMAIN.
   - DOMAIN is empty: install cloudflared and run cloudflared tunnel --url http://localhost:8000 as a systemd service that starts on boot. HOST is the https://<name>.trycloudflare.com host from its log.
   Wait until curl -fsS https://$HOST/health returns {"ok":true}.
6. For each repo in REPOS, call the GitHub REST API with WEBHOOK_TOKEN. Create a webhook, or update an existing one that points to this service (the same URL, or an older trycloudflare.com URL):
   {"name":"web","active":true,"events":["pull_request","issue_comment"],"config":{"url":"https://$HOST/webhook","content_type":"json","secret":"<GITHUB_WEBHOOK_SECRET from .env>","insecure_ssl":"0"}}
7. Verify each webhook: call POST /repos/{repo}/hooks/{id}/pings, then GET /repos/{repo}/hooks/{id}/deliveries. The ping delivery must have status_code 200. A 401 means the secret in the webhook doesn't match .env: fix it and ping again.
8. Report HOST, the webhook id for each repo, whether the URL is temporary, and anything I still have to do myself, such as opening ports 80 and 443 in the cloud provider's firewall.
```

## Setup

### 1. Tokens

- **GitHub:** create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new). Resource owner: the user or organization that owns the repos. Repository access: the repos to review. Permissions: **Contents: Read** and **Pull requests: Read and write**. A fine-grained token covers the repos of one owner.
- **Claude:** set exactly one. Run `claude setup-token` to use your Claude plan (`CLAUDE_CODE_OAUTH_TOKEN`), or create an API key in the Anthropic Console for API billing (`ANTHROPIC_API_KEY`, wins if both are set).
- **Webhook secret:** a random hex string, for example from `openssl rand -hex 32`. Avoid `$` in `.env` values; compose expands it.

### 2. Start the service

```sh
cp .env.example .env          # fill in the values
docker compose up -d --build
sleep 3; curl http://localhost:8000/health   # {"ok":true}
```

### 3. Make it reachable for GitHub

The port is bound to `127.0.0.1`, so put a tunnel or an HTTPS reverse proxy (for example Caddy) in front of it. For testing, a tunnel is quickest:

```sh
cloudflared tunnel --url http://localhost:8000   # prints https://<random>.trycloudflare.com
# or: ngrok http 8000
```

### 4. Add the webhook

In the repo, or once in the organization to cover all its repos: **Settings → Webhooks → Add webhook**

- Payload URL: `https://<your-host>/webhook`
- Content type: `application/json`
- Secret: the value of `GITHUB_WEBHOOK_SECRET`
- Events: **Let me select individual events** → **Pull requests** and **Issue comments**

GitHub sends a ping right away; it should show a green check under **Recent Deliveries**.

### 5. Try it

Open a PR. The review comment appears within a few minutes. Comment `@claude review` to review the PR's current head again, for example after new commits.

**Another repo** only needs the webhook (or the org webhook) and token access. No code or service change. `ALLOWED_REPOS` can restrict which repos are reviewed.

## Configuration

All settings come from `.env` (see `.env.example`).

| Variable | Default | |
| --- | --- | --- |
| `GITHUB_WEBHOOK_SECRET` | required | Webhook secret |
| `GH_TOKEN` | required | GitHub token |
| `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | one required | Claude auth |
| `ALLOWED_REPOS` | empty: all repos | Comma-separated `owner/repo` list |
| `REVIEW_PROMPT_FILE` | `/app/review_prompt.md` | Review instructions; `{language}` is replaced |
| `REVIEW_LANGUAGE` | `German` | Language of the review |
| `CLAUDE_ALLOWED_TOOLS` | `Read,Grep,Glob` | The only tools the agent gets |
| `CLAUDE_DISALLOWED_TOOLS` | `Bash,Edit,Write,WebFetch,WebSearch` | |
| `CLAUDE_MODEL` | CLI default | |
| `CLAUDE_MAX_TURNS` | `20` | |
| `CLAUDE_EXTRA_ARGS` | empty | Appended to the command, shell-style quoting |
| `MAX_CONCURRENT_REVIEWS` | `2` | |
| `MAX_DIFF_CHARS` | `150000` | Longer diffs are truncated |
| `REVIEW_TIMEOUT` | `900` | Seconds per review |

The service runs:

```
claude -p --tools … --allowedTools … --disallowedTools … --max-turns N --restricted --strict-mcp-config [--model M] [extra args] -- "<prompt>"
```

with the PR title, description and diff on stdin.

The review instructions are in `prompt/review_prompt.md`. The `prompt` folder is mounted into the container, so edits apply to the next review without a rebuild or restart.

**Repo-specific rules** belong in the reviewed repo: the agent reads its `REVIEW.md` or `CLAUDE.md`.

## Security

- PR content is untrusted. The agent only gets Read, Grep and Glob, and runs with `--restricted` and `--strict-mcp-config`: the PR's `.claude/settings.json` (hooks, env), `.mcp.json` and auto-loaded `CLAUDE.md` are ignored and file access stays inside the checkout. Symlinks are checked out as plain files. The prompt tells the model to ignore instructions in the PR.
- The agent's environment holds only the Claude credential, never `GH_TOKEN`. The service makes its own environment unreadable to the agent, which runs as the same user. Don't add `init: true` to the compose file; that would undo it.
- Git authenticates with an `http.extraheader` passed through `GIT_CONFIG_*` variables, never through URLs or arguments.
- The service posts the comment, not the agent, and redacts known secret values from it.

Known limits:

- A PR can edit its own `REVIEW.md`/`CLAUDE.md` and so steer its review.
- Every new PR is reviewed, including PRs from forks, and uses your Claude quota. Use `ALLOWED_REPOS` or private repos if that matters.
- GitHub reports private organization members as `CONTRIBUTOR`, so they can only use `@claude review` once their membership is public or they are collaborators.
- GitHub refuses diffs over 300 files or 20000 changed lines; such PRs can't be reviewed.
- Which commits were reviewed is kept in memory only; after a restart a commit may be reviewed again.

## Development

```sh
uv run --no-project --python 3.12 --with-requirements requirements.txt python -m unittest
```

The repo is public. Keep secrets in `.env` only; `.gitignore` excludes it.

- Install the [gitleaks](https://github.com/gitleaks/gitleaks) pre-commit hook once per clone: `uv tool install pre-commit && pre-commit install`.
- Keep GitHub secret scanning and push protection enabled in the repo's security settings.
- If a secret is ever committed, rotate it immediately. Cleaning the git history is not enough.
