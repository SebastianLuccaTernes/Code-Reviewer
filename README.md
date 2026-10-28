# Self-hosted PR Reviewer

When a pull request is opened on GitHub, Claude reviews it and posts the result as a PR comment. It runs in its own container and uses no GitHub Actions minutes.

A review starts when:

- a non-draft PR is opened or marked ready for review (each commit is reviewed automatically at most once), or
- an OWNER, MEMBER or COLLABORATOR comments `@claude review` on a PR.

The service verifies the webhook signature, answers right away, then in the background fetches the PR and its diff, checks out the head commit into a temporary directory, runs `claude -p` there with read-only tools, posts the result as a comment and deletes the directory.

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
| `REVIEW_PROMPT_FILE` | `/app/review_prompt.md` | Review instructions; `{language}` is replaced. `/app/prompt/ponytail_review.md` for the ponytail review |
| `REVIEW_LANGUAGE` | `German` | Language of the review |
| `CLAUDE_ALLOWED_TOOLS` | `Read,Grep,Glob` | The only tools the agent gets |
| `CLAUDE_DISALLOWED_TOOLS` | `Bash,Edit,Write,WebFetch,WebSearch` | |
| `CLAUDE_MODEL` | CLI default | |
| `CLAUDE_MAX_TURNS` | `40` | |
| `CLAUDE_EXTRA_ARGS` | empty | Appended to the command, shell-style quoting |
| `MAX_CONCURRENT_REVIEWS` | `2` | |
| `MAX_DIFF_CHARS` | `150000` | Longer diffs are truncated |
| `REVIEW_TIMEOUT` | `1800` | Seconds per review |

The service runs:

```
claude -p --tools … --allowedTools … --disallowedTools … --max-turns N --restricted --strict-mcp-config [--model M] [extra args] -- "<prompt>"
```

with the PR title, description and diff on stdin.

The review instructions are in `prompt/review_prompt.md`. The rules in `prompt/rules.md` (untrusted PR content, no secrets in the comment) always come first, whatever prompt file is set. The `prompt` folder is mounted into the container, so edits apply to the next review without a rebuild or restart.

`prompt/ponytail_review.md` is an alternative: the [ponytail](https://github.com/DietrichGebert/ponytail) review (MIT, see `prompt/LICENSE-ponytail`) adapted to this service. It explains each finding in plain language, groups findings into must fix, should fix and nice to have, and also flags code that could be deleted or reused. Set `REVIEW_PROMPT_FILE=/app/prompt/ponytail_review.md` in `.env` and run `docker compose up -d` to use it.

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
