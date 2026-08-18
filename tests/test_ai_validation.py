import copy
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from types import SimpleNamespace


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("DATABASE_PATH", os.path.join(_TEMP_DIR.name, "calories.sqlite3"))
os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LLM_API_KEY", "test")
os.environ.setdefault("LLM_MODEL", "test")

import index  # noqa: E402


VALID_RESPONSE = {
    "items": [
        {
            "meal_type": "lunch",
            "name": "Рис*",
            "weight_g": 200.0,
            "kcal": 260,
            "protein_g": 5.4,
            "fat_g": 0.6,
            "carb_g": 56.0,
            "portion_note": "оценка: готовый вес",
        },
        {
            "meal_type": "lunch",
            "name": "Куриная грудка",
            "weight_g": 150.0,
            "kcal": 248,
            "protein_g": 46.5,
            "fat_g": 5.4,
            "carb_g": 0.0,
            "portion_note": "",
        },
    ],
    "total_kcal": 508,
    "total": {"protein_g": 51.9, "fat_g": 6.0, "carb_g": 56.0},
    "tip": "Оценка зависит от способа приготовления грудки.",
}


class AiValidationTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        _TEMP_DIR.cleanup()

    def response(self):
        return copy.deepcopy(VALID_RESPONSE)

    def test_accepts_consistent_response(self):
        self.assertTrue(index.validate_ai_response(self.response()))

    def test_rejects_string_instead_of_number(self):
        response = self.response()
        response["items"][0]["kcal"] = "260"
        self.assertFalse(index.validate_ai_response(response))

    def test_rejects_wrong_calorie_total(self):
        response = self.response()
        response["total_kcal"] = 700
        self.assertFalse(index.validate_ai_response(response))

    def test_rejects_wrong_macro_total(self):
        response = self.response()
        response["total"]["protein_g"] = 30.0
        self.assertFalse(index.validate_ai_response(response))

    def test_rejects_macros_larger_than_weight(self):
        response = self.response()
        response["items"][0]["protein_g"] = 250.0
        response["total"]["protein_g"] = 296.5
        self.assertFalse(index.validate_ai_response(response))

    def test_rejects_impossible_calorie_density(self):
        response = self.response()
        response["items"][0]["kcal"] = 3000
        response["total_kcal"] = 3248
        self.assertFalse(index.validate_ai_response(response))

    def test_rejects_too_many_items(self):
        response = self.response()
        response["items"] *= index.MAX_AI_ITEMS
        response["items"].append(copy.deepcopy(response["items"][0]))
        self.assertFalse(index.validate_ai_response(response))

    def test_rejects_overlong_model_text(self):
        response = self.response()
        response["tip"] = "x" * (index.MAX_AI_TIP_LENGTH + 1)
        self.assertFalse(index.validate_ai_response(response))

    def test_accepts_empty_non_food_response(self):
        response = {
            "items": [],
            "total_kcal": 0,
            "total": {"protein_g": 0, "fat_g": 0, "carb_g": 0},
            "tip": "Напиши, что ел.",
        }
        self.assertTrue(index.validate_ai_response(response))

    def test_temperature_is_omitted_by_default(self):
        captured = {}

        class FakeClient:
            chat = SimpleNamespace(completions=SimpleNamespace(
                create=lambda **params: captured.update(params) or SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
                )
            ))

        original_client = index._get_ai_client
        original_temperature = index.LLM_TEMPERATURE_RAW
        try:
            index._get_ai_client = lambda: FakeClient()
            index.LLM_TEMPERATURE_RAW = ""
            index._request_completion("тест", structured=False)
        finally:
            index._get_ai_client = original_client
            index.LLM_TEMPERATURE_RAW = original_temperature

        self.assertNotIn("temperature", captured)

    def test_gemini_compatible_schema_and_fallback_template(self):
        schema = json.dumps(index.OUTPUT_SCHEMA)
        self.assertNotIn("minLength", schema)
        self.assertNotIn("maxLength", schema)
        self.assertIn('"total":{"protein_g"', index.DEFAULT_SYSTEM_PROMPT)

    def test_webhook_rejects_missing_secret(self):
        with self.assertRaises(PermissionError):
            index.process_telegram_update({"update_id": 1}, {})

    def test_webhook_update_is_processed_once(self):
        calls = []
        original_route = index.route_message
        try:
            index.route_message = lambda message: calls.append(message)
            update = {
                "update_id": 987654321,
                "message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/start"},
            }
            headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
            index.process_telegram_update(update, headers)
            index.process_telegram_update(update, headers)
        finally:
            index.route_message = original_route
        self.assertEqual(calls, [update["message"]])

    def test_external_text_is_escaped_for_markdown(self):
        self.assertEqual(index._escape_markdown("a_*[]()`\\"), "a\\_\\*\\[\\]\\(\\)\\`\\\\")

    def test_database_files_are_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(index.DATABASE_PATH).st_mode), 0o600)

    def test_failed_rewrite_preserves_existing_day(self):
        user_id, date_utc = 42, "2026-08-18"
        totals = {"kcal": 100, "protein_g": 1, "fat_g": 2, "carb_g": 3}
        index.save_record(user_id, "старый рацион", "{}", totals, date_utc)
        original_insert = index._insert_record
        try:
            def fail_insert(*args, **kwargs):
                raise sqlite3.OperationalError("disk full")
            index._insert_record = fail_insert
            with self.assertRaises(sqlite3.OperationalError):
                index.replace_day(user_id, "новый рацион", "{}", totals, date_utc)
        finally:
            index._insert_record = original_insert
        self.assertEqual(index.count_day(user_id, date_utc), 1)

    def test_food_request_reservation_enforces_limit(self):
        user_id, date_utc = 99, "2026-08-19"
        first = index.reserve_food_request(user_id, date_utc, limit=2)
        second = index.reserve_food_request(user_id, date_utc, limit=2)
        try:
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertIsNone(index.reserve_food_request(user_id, date_utc, limit=2))
        finally:
            index.release_food_request(first)
            index.release_food_request(second)


if __name__ == "__main__":
    unittest.main()
