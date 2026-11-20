import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import (
    Receipts,
    load_config,
    matching_rules,
    render_message,
    send_with_retry,
    source_link,
)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.rules = [
            {
                "name": "housing",
                "keywords": ["сниму квартиру"],
                "antiwords": ["предлагаем работу"],
            }
        ]

    def test_request_and_advertisement(self):
        self.assertEqual(
            matching_rules("Привет! СНИМУ квартиру на месяц", self.rules), ["housing"]
        )
        self.assertEqual(
            matching_rules("Сниму квартиру. Предлагаем работу с жильём", self.rules), []
        )

    def test_phrase_boundaries_and_spaces(self):
        self.assertEqual(matching_rules("сниму   квартиру", self.rules), ["housing"])
        self.assertEqual(matching_rules("сниму квартирулю", self.rules), [])

    def test_antiword_applies_only_to_its_rule(self):
        rules = self.rules + [
            {"name": "jobs", "keywords": ["предлагаем работу"], "antiwords": []}
        ]
        self.assertEqual(
            matching_rules("Сниму квартиру; предлагаем работу", rules), ["jobs"]
        )


class StorageTests(unittest.TestCase):
    def test_receipts_survive_restart(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.db"
            with Receipts(path) as receipts:
                self.assertFalse(receipts.seen(-10012, 23))
                receipts.remember(-10012, 23)
            with Receipts(path) as receipts:
                self.assertTrue(receipts.seen(-10012, 23))
                self.assertFalse(receipts.seen(-10013, 23))


class ConfigTests(unittest.TestCase):
    def test_links_depend_on_chat_type(self):
        self.assertIsNone(source_link(-100123, 7))
        self.assertEqual(
            source_link(-100123, 7, username="example"), "https://t.me/example/7"
        )
        self.assertEqual(
            source_link(-1001234567890, 7, is_channel=True),
            "https://t.me/c/1234567890/7",
        )

    def test_config_validation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "config.json"
            config = {
                "chats": ["@example"],
                "rules": [{"name": "a", "keywords": ["ищу"], "antiwords": []}],
            }
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(load_config(path), config)
            config["rules"][0]["antiwords"] = "строка вместо списка"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_empty_chats_and_duplicate_names_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"chats": [], "rules": []}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)
            rule = {"name": "a", "keywords": ["ищу"], "antiwords": []}
            path.write_text(
                json.dumps({"chats": [123], "rules": [rule, rule]}), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_config(path)

    def test_long_emoji_text_stays_within_telegram_limit(self):
        result = render_message(
            "Demo", ["housing"], "😀" * 5000, "https://t.me/example/1"
        )
        self.assertLessEqual(len(result.encode("utf-16-le")) // 2, 4096)
        self.assertTrue(result.endswith("https://t.me/example/1"))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_respected_on_last_attempt(self):
        from aiogram.exceptions import TelegramRetryAfter

        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=TelegramRetryAfter(
                    method=None, message="wait", retry_after=17
                )
            )
        )
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(TelegramRetryAfter):
                await send_with_retry(bot, 123, "test")
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [17, 17, 17])

    async def test_network_retry_then_success(self):
        from aiogram.exceptions import TelegramNetworkError

        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[TelegramNetworkError(method=None, message="offline"), None]
            )
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await send_with_retry(bot, 123, "test")
        self.assertEqual(bot.send_message.await_count, 2)


if __name__ == "__main__":
    unittest.main()
