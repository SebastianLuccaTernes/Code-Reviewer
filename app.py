"""Self-hosted PR reviewer: GitHub webhook in, `claude -p` review out as a PR comment."""

import hashlib
import hmac
import json
import logging
import os
import re
import sys

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reviewer")

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET:
    sys.exit("Set GITHUB_WEBHOOK_SECRET")
ALLOWED_REPOS = {r.strip().lower() for r in os.getenv("ALLOWED_REPOS", "").split(",") if r.strip()}

TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}

app = FastAPI()
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
    log.info("review requested for %s#%s (manual=%s)", repo, number, manual)
