"""Run with: python -m unittest. Uses fake values only."""

import hashlib
import hmac
import json
import os
import unittest

os.environ.update(GITHUB_WEBHOOK_SECRET="test-secret")

from fastapi.testclient import TestClient

import app


def sign(body, secret="test-secret"):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookTest(unittest.TestCase):
    client = TestClient(app.app)

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
