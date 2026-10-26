"""Run with: python -m unittest. Uses fake values only."""

import hashlib
import hmac
import json
import os
import unittest
from unittest import mock

os.environ.update(GITHUB_WEBHOOK_SECRET="test-secret")

from fastapi.testclient import TestClient

import app


def sign(body, secret="test-secret"):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_event(action="opened", draft=False, sha="abc123", repo="acme/widgets"):
    return {"action": action, "repository": {"full_name": repo},
            "pull_request": {"number": 7, "draft": draft, "head": {"sha": sha}}}


def comment_event(body="@claude review", association="MEMBER", user_type="User", on_pr=True):
    issue = {"number": 7, "pull_request": {}} if on_pr else {"number": 7}
    return {"action": "created", "repository": {"full_name": "acme/widgets"}, "issue": issue,
            "comment": {"body": body, "author_association": association, "user": {"type": user_type}}}


class WebhookTest(unittest.TestCase):
    client = TestClient(app.app)

    def setUp(self):
        app.reviewed.clear()

    def post(self, body, signature, event="ping"):
        return self.client.post("/webhook", content=body,
                                headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature})

    def test_valid_signature_is_accepted(self):
        body = json.dumps({"zen": "Keep it simple."}).encode()
        self.assertEqual(self.post(body, sign(body)).status_code, 200)

    def test_invalid_or_missing_signature_is_401(self):
        body = b'{"zen": "Keep it simple."}'
        self.assertEqual(self.post(body, sign(body, "wrong-secret")).status_code, 401)
        self.assertEqual(self.post(body, sign(b"another body")).status_code, 401)
        self.assertEqual(self.post(body, "sha256=\xe4".encode("latin-1")).status_code, 401)
        self.assertEqual(self.client.post("/webhook", content=body).status_code, 401)

    def test_form_encoded_payload_is_400(self):
        body = b"payload=%7B%7D"
        self.assertEqual(self.post(body, sign(body)).status_code, 400)

    def test_trigger_schedules_a_background_review(self):
        body = json.dumps(pr_event()).encode()
        with mock.patch.object(app, "review", mock.AsyncMock()) as review:
            self.assertEqual(self.post(body, sign(body), "pull_request").status_code, 200)
        review.assert_awaited_once_with("acme/widgets", 7, False)


class TriggerTest(unittest.TestCase):
    def setUp(self):
        app.reviewed.clear()

    def test_opened_or_ready_pr_is_reviewed_once_per_commit(self):
        self.assertEqual(app.trigger("pull_request", pr_event()), ("acme/widgets", 7, False))
        self.assertIsNone(app.trigger("pull_request", pr_event("ready_for_review")))
        self.assertEqual(app.trigger("pull_request", pr_event("ready_for_review", sha="def456")),
                         ("acme/widgets", 7, False))

    def test_drafts_and_other_events_are_skipped(self):
        self.assertIsNone(app.trigger("pull_request", pr_event(draft=True)))
        self.assertIsNone(app.trigger("pull_request", pr_event("synchronize")))
        self.assertIsNone(app.trigger("ping", {"zen": "Keep it simple."}))

    def test_manual_trigger_needs_a_trusted_human_on_a_pr(self):
        for body in ("@claude review", "  @Claude Review please", "@claude review."):
            self.assertEqual(app.trigger("issue_comment", comment_event(body)), ("acme/widgets", 7, True))
        for event in (comment_event(association="CONTRIBUTOR"), comment_event(association="NONE"),
                      comment_event(user_type="Bot"), comment_event(on_pr=False),
                      comment_event("please @claude review"), comment_event("@claude reviewer")):
            self.assertIsNone(app.trigger("issue_comment", event))

    def test_allowed_repos_restricts_triggers(self):
        with mock.patch.object(app, "ALLOWED_REPOS", {"acme/widgets"}):
            self.assertIsNotNone(app.trigger("pull_request", pr_event(repo="Acme/Widgets")))
            self.assertIsNone(app.trigger("pull_request", pr_event(repo="acme/other")))
