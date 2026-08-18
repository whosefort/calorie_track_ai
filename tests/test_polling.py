import os
import tempfile
import unittest
from unittest.mock import patch


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("DATABASE_PATH", os.path.join(_TEMP_DIR.name, "calories.sqlite3"))
os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("LLM_API_KEY", "test")
os.environ.setdefault("LLM_MODEL", "test")
os.environ.setdefault("TELEGRAM_MODE", "polling")

import polling  # noqa: E402


class PollingTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        _TEMP_DIR.cleanup()

    def test_confirms_processed_updates_with_next_offset(self):
        updates = [{"update_id": 7}, {"update_id": 8}]
        with patch.object(polling, "process_polled_update") as process:
            offset = polling.process_updates(updates, None)
        self.assertEqual(offset, 9)
        self.assertEqual(process.call_count, 2)

    def test_invalid_update_does_not_advance_offset(self):
        with patch.object(polling, "process_polled_update") as process:
            offset = polling.process_updates([{"update_id": True}], 10)
        self.assertEqual(offset, 10)
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
