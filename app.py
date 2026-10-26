"""Self-hosted PR reviewer: GitHub webhook in, `claude -p` review out as a PR comment."""

import hashlib
import hmac
import os
import sys

from fastapi import FastAPI, HTTPException, Request

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET:
    sys.exit("Set GITHUB_WEBHOOK_SECRET")

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode(), request.headers.get("x-hub-signature-256", "").encode()):
        raise HTTPException(401, "invalid signature")
    return {"ok": True}
