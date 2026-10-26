"""Self-hosted PR reviewer: GitHub webhook in, `claude -p` review out as a PR comment."""

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}
