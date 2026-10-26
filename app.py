"""Self-hosted PR reviewer: GitHub webhook in, `claude -p` review out as a PR comment."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reviewer")

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GH_TOKEN = os.getenv("GH_TOKEN", "")
if not (WEBHOOK_SECRET and GH_TOKEN):
    sys.exit("Set GITHUB_WEBHOOK_SECRET and GH_TOKEN")
ALLOWED_REPOS = {r.strip().lower() for r in os.getenv("ALLOWED_REPOS", "").split(",") if r.strip()}
REVIEW_TIMEOUT = int(os.getenv("REVIEW_TIMEOUT", "900"))

TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
GIT_AUTH = base64.b64encode(f"x-access-token:{GH_TOKEN}".encode()).decode()

app = FastAPI()
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_REVIEWS", "2")))
reviewed = set()  # "owner/repo@sha" already reviewed; in memory only


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request, tasks: BackgroundTasks):
    body = await request.body()
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
            await run_review(repo, number)
    except Exception:
        log.exception("review of %s#%s failed", repo, number)


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
        log.info("checked out %s@%s (%d character diff)", repo, sha, len(diff))


async def run(cmd, env):
    proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    finally:
        if proc.returncode is None:  # cancelled by the review timeout
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError(f"{cmd[0]} exited with {proc.returncode}: {(err + out).decode(errors='replace')[-500:]}")
    return out.decode(errors="replace")
