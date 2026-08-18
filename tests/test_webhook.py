import os
import tempfile
import unittest


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("DATABASE_PATH", os.path.join(_TEMP_DIR.name, "calories.sqlite3"))
os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LLM_API_KEY", "test")
os.environ.setdefault("LLM_MODEL", "test")

from fastapi.testclient import TestClient  # noqa: E402
import app as webapp  # noqa: E402


class WebhookTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        _TEMP_DIR.cleanup()

    def setUp(self):
        self.client = TestClient(webapp.app)
        self.original_process = webapp.process_telegram_update
        self.calls = []
        webapp.process_telegram_update = lambda payload, headers: self.calls.append(payload)

    def tearDown(self):
        webapp.process_telegram_update = self.original_process

    def test_rejects_missing_secret_before_body_processing(self):
        response = self.client.post("/telegram/webhook", content=b"{}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_rejects_oversized_body(self):
        response = self.client.post(
            "/telegram/webhook",
            content=b"x" * (webapp.WEBHOOK_BODY_LIMIT + 1),
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.calls, [])

    def test_passes_valid_object_to_processor(self):
        payload = {"update_id": 1}
        response = self.client.post(
            "/telegram/webhook",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, [payload])


if __name__ == "__main__":
    unittest.main()
