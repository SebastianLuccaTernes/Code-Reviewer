"""Self-hosted PR reviewer: GitHub webhook in, `claude -p` review out as a PR comment."""

import asyncio
import base64
import contextlib
import ctypes
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import signal
import sys
import tempfile

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reviewer")

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GH_TOKEN = os.getenv("GH_TOKEN", "")
# API billing wins over plan usage when both are set.
CLAUDE_AUTH_VAR = "ANTHROPIC_API_KEY" if os.getenv("ANTHROPIC_API_KEY") else "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_AUTH = os.getenv(CLAUDE_AUTH_VAR, "")
if not (WEBHOOK_SECRET and GH_TOKEN and CLAUDE_AUTH):
    sys.exit("Set GITHUB_WEBHOOK_SECRET, GH_TOKEN and CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY")
ALLOWED_REPOS = {r.strip().lower() for r in os.getenv("ALLOWED_REPOS", "").split(",") if r.strip()}
REVIEW_PROMPT_FILE = os.getenv("REVIEW_PROMPT_FILE", "/app/review_prompt.md")
REVIEW_LANGUAGE = os.getenv("REVIEW_LANGUAGE", "German")
CLAUDE_ALLOWED_TOOLS = os.getenv("CLAUDE_ALLOWED_TOOLS", "Read,Grep,Glob")
CLAUDE_DISALLOWED_TOOLS = os.getenv("CLAUDE_DISALLOWED_TOOLS", "Bash,Edit,Write,WebFetch,WebSearch")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")
CLAUDE_MAX_TURNS = os.getenv("CLAUDE_MAX_TURNS", "40")
CLAUDE_EXTRA_ARGS = os.getenv("CLAUDE_EXTRA_ARGS", "")
MAX_DIFF_CHARS = int(os.getenv("MAX_DIFF_CHARS", "150000"))
REVIEW_TIMEOUT = int(os.getenv("REVIEW_TIMEOUT", "1800"))

RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt/rules.md")  # the mounted folder
MARKER = "<!-- claude-review-bot -->"
MAX_COMMENT_CHARS = 60000
MAX_PAYLOAD_BYTES = 2 * 2**20  # pull_request/issue_comment payloads stay under 1 MB (texts max 65536 chars)
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
GIT_AUTH = base64.b64encode(f"x-access-token:{GH_TOKEN}".encode()).decode()

# The agent runs as the same user; a non-dumpable process keeps its /proc/<pid>/environ (GH_TOKEN) from it.
if sys.platform == "linux":
    ctypes.CDLL(None).prctl(4, 0)  # PR_SET_DUMPABLE = 0

app = FastAPI()
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_REVIEWS", "2")))
reviewed = set()  # "owner/repo@sha" already reviewed; in memory only


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request, tasks: BackgroundTasks):
    body = bytearray()
    async for chunk in request.stream():  # capped: anyone can send a body before the signature is checked
        body += chunk
        if len(body) > MAX_PAYLOAD_BYTES:
            raise HTTPException(413, "payload too large")
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode(), request.headers.get("x-hub-signature-256", "").encode()):
        raise HTTPException(401, "invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "set the webhook content type to application/json")
    job = trigger(request.headers.get("x-github-event"), payload)
    if job:
        tasks.add_task(review, *job)
    return {"ok": True}


def trigger(event, payload):
    """Return (repo, PR number, manual) if this event should start a review."""
    repo = payload.get("repository", {}).get("full_name", "")
    if ALLOWED_REPOS and repo.lower() not in ALLOWED_REPOS:
        return None
    if event == "pull_request" and payload["action"] in ("opened", "ready_for_review"):
        pr = payload["pull_request"]
        commit = f"{repo}@{pr['head']['sha']}"
        if pr["draft"] or commit in reviewed:
            return None
        reviewed.add(commit)
        return repo, pr["number"], False
    if event == "issue_comment" and payload["action"] == "created" and "pull_request" in payload["issue"]:
        comment = payload["comment"]
        if (re.match(r"\s*@claude\s+review\b", comment["body"], re.IGNORECASE)
                and comment["author_association"] in TRUSTED and comment["user"]["type"] != "Bot"):
            return repo, payload["issue"]["number"], True
    return None


async def review(repo, number, manual):
    try:
        async with slots, asyncio.timeout(REVIEW_TIMEOUT):
            text = await run_review(repo, number)
        await post_comment(repo, number, text)
        log.info("posted review on %s#%s", repo, number)
    except Exception as e:
        log.exception("review of %s#%s failed", repo, number)
        if manual:  # someone asked for this review, so tell them; automatic reviews fail quietly
            try:
                await post_comment(repo, number, f"Review failed: {str(e) or type(e).__name__}")
            except Exception:  # e.g. GitHub is down too; an escaping error would drop the server's connection
                log.exception("could not report the failure on %s#%s", repo, number)


def github():
    return httpx.AsyncClient(base_url="https://api.github.com", timeout=60, headers={
        "Authorization": f"Bearer {GH_TOKEN}", "X-GitHub-Api-Version": "2026-03-10"})


async def run_review(repo, number):
    async with github() as gh:
        pr = (await gh.get(f"/repos/{repo}/pulls/{number}")).raise_for_status().json()
        diff = (await gh.get(f"/repos/{repo}/pulls/{number}",
                             headers={"Accept": "application/vnd.github.diff"})).raise_for_status().text
    sha = pr["head"]["sha"]
    reviewed.add(f"{repo}@{sha}")
    log.info("reviewing %s#%s at %s", repo, number, sha)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n[diff truncated after {MAX_DIFF_CHARS} characters]"
    # Read per review, so prompt edits apply without a restart. The rules come first, whatever prompt is set.
    with open(RULES_FILE) as rules, open(REVIEW_PROMPT_FILE) as f:
        prompt = f"{rules.read()}\n{f.read()}".replace("{language}", REVIEW_LANGUAGE)
    pr_text = (f"<pr_title>\n{pr['title']}\n</pr_title>\n<pr_description>\n{pr['body'] or ''}\n"
               f"</pr_description>\n<diff>\n{diff}\n</diff>\n")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        src, home = f"{tmp}/src", f"{tmp}/home"  # fresh HOME per review: no state shared between reviews
        os.mkdir(home)
        env = {"PATH": os.environ["PATH"], "HOME": home}
        await run(["git", "init", "-q", src], env)
        await run(["git", "-C", src, "fetch", "-q", "--depth=1", f"https://github.com/{repo}.git", sha],
                  env | {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                         "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                         "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {GIT_AUTH}"})
        # Symlinks become plain files, so the PR can't point anything at files outside the checkout.
        await run(["git", "-C", src, "-c", "core.symlinks=false", "checkout", "-q", "FETCH_HEAD"], env)
        text = await run(claude_command(prompt), env | {CLAUDE_AUTH_VAR: CLAUDE_AUTH}, cwd=src, stdin=pr_text)
    if not text.strip():
        raise RuntimeError("claude returned an empty review")
    return text


def claude_command(prompt):
    # --allowedTools only pre-approves tools; --tools removes all others (Agent, Skill, ...).
    # The checkout is untrusted: --restricted ignores its settings, hooks and CLAUDE.md and keeps the file
    # tools inside it; --strict-mcp-config ignores its .mcp.json.
    cmd = ["claude", "-p", "--tools", CLAUDE_ALLOWED_TOOLS, "--allowedTools", CLAUDE_ALLOWED_TOOLS,
           "--disallowedTools", CLAUDE_DISALLOWED_TOOLS, "--max-turns", CLAUDE_MAX_TURNS, "--restricted",
           "--strict-mcp-config"]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    # The prompt goes last, after "--", so a prompt starting with "-" (front matter, a list) isn't read as an option.
    return cmd + shlex.split(CLAUDE_EXTRA_ARGS) + ["--", prompt]


async def run(cmd, env, cwd=None, stdin=""):
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                start_new_session=True)
    try:
        out, err = await proc.communicate(stdin.encode())
    finally:
        if proc.returncode is None:  # cancelled by the review timeout
            # Kill the whole group: children like git-remote-https hold the pipes, and wait() waits for those too.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
    if proc.returncode:
        # claude prints its errors (e.g. "Reached max turns") on stdout, git on stderr
        raise RuntimeError(f"{cmd[0]} exited with {proc.returncode}: {(err + out).decode(errors='replace')[-500:]}")
    return out.decode(errors="replace")


async def post_comment(repo, number, text):
    body = f"{MARKER}\n{text}"
    for secret in (WEBHOOK_SECRET, GH_TOKEN, GIT_AUTH, CLAUDE_AUTH):
        body = body.replace(secret, "[redacted]")  # last line of defense if the agent was talked into leaking one
    async with github() as gh:
        r = await gh.post(f"/repos/{repo}/issues/{number}/comments", json={"body": body[:MAX_COMMENT_CHARS]})
        r.raise_for_status()
