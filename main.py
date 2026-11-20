"""One account, selected chats, text rules, and notifications to a Telegram bot."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

log = logging.getLogger("monitor")


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def contains(text, phrase):
    return bool(re.search(r"(?<!\w)" + re.escape(normalize(phrase)) + r"(?!\w)", text))


def matching_rules(text, rules):
    text = normalize(text)
    return [
        rule["name"]
        for rule in rules
        if any(contains(text, phrase) for phrase in rule["keywords"])
        and not any(contains(text, phrase) for phrase in rule["antiwords"])
    ]


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object")
    chats, rules = config.get("chats"), config.get("rules")
    if not isinstance(chats, list) or not chats:
        raise ValueError("Specify at least one source chat")
    if any(
        type(chat) is not int and (not isinstance(chat, str) or not chat.strip())
        for chat in chats
    ):
        raise ValueError("Chat must be an ID or username")
    if not isinstance(rules, list) or not rules:
        raise ValueError("Specify at least one rule")
    names = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Rule must be an object")
        name = rule.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Rule names must be nonempty and unique")
        names.add(name)
        for key in ("keywords", "antiwords"):
            values = rule.get(key)
            if not isinstance(values, list) or any(
                not isinstance(v, str) or not normalize(v) for v in values
            ):
                raise ValueError(f"{key} must be a list of nonempty phrases")
        if not rule["keywords"]:
            raise ValueError("Rule needs at least one keyword")
    return config


class Receipts:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sent (chat_id INTEGER, message_id INTEGER, "
            "PRIMARY KEY (chat_id, message_id))"
        )
        self.db.commit()

    def seen(self, chat_id, message_id):
        return (
            self.db.execute(
                "SELECT 1 FROM sent WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
            is not None
        )

    def remember(self, chat_id, message_id):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO sent VALUES (?,?)", (chat_id, message_id)
            )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()


def clip(text, units):
    return text.encode("utf-16-le")[: units * 2].decode("utf-16-le", errors="ignore")


def source_link(chat_id, message_id, username=None, is_channel=False):
    if username:
        return f"https://t.me/{username}/{message_id}"
    if is_channel:
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return None


def render_message(title, names, text, link):
    header = f"Чат: {clip(title, 120)}\nПравила: {clip(', '.join(names), 400)}\n\n"
    footer = f"\n\n{link}" if link else ""
    budget = 4000 - len((header + footer).encode("utf-16-le")) // 2
    return header + clip(text, max(0, budget)) + footer


async def send_with_retry(bot, destination, text):
    from aiogram.exceptions import (
        TelegramNetworkError,
        TelegramRetryAfter,
        TelegramServerError,
    )

    for attempt in range(3):
        try:
            await bot.send_message(destination, text, parse_mode=None)
            return
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            if attempt == 2:
                raise
        except (TelegramNetworkError, TelegramServerError):
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)


async def run():
    from aiogram import Bot
    from dotenv import load_dotenv
    from telethon import TelegramClient, events, types

    load_dotenv(Path(".env"))
    config = load_config("config.json")
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    destination = int(os.environ["RESULT_CHAT_ID"])
    token = os.environ["BOT_TOKEN"]
    Path("data").mkdir(exist_ok=True)
    client = TelegramClient("data/user", api_id, api_hash)
    bot = Bot(token)
    try:
        await bot.get_me()
        await client.start()  # First run asks for phone, login code and optional 2FA.
        sources = []
        for chat in config["chats"]:
            entity = await client.get_entity(chat)
            if not isinstance(entity, (types.Chat, types.Channel)):
                raise ValueError("Sources must be groups or channels")
            sources.append(entity)
        lock = asyncio.Lock()
        with Receipts("data/state.db") as receipts:

            async def on_message(event):
                text = event.raw_text or ""
                if event.chat_id == destination:
                    return
                names = matching_rules(text, config["rules"])
                if not names:
                    return
                async with lock:
                    if receipts.seen(event.chat_id, event.id):
                        return
                    try:
                        chat = await event.get_chat()
                        link = source_link(
                            event.chat_id,
                            event.id,
                            getattr(chat, "username", None),
                            isinstance(chat, types.Channel),
                        )
                        message = render_message(chat.title, names, text, link)
                        await send_with_retry(bot, destination, message)
                        receipts.remember(event.chat_id, event.id)
                        log.info("Notification sent")
                    except Exception as exc:
                        log.error("Notification failed: %s", type(exc).__name__)

            client.add_event_handler(
                on_message, events.NewMessage(chats=sources, incoming=True)
            )
            log.info("Listening to %s chats. Ctrl+C to stop.", len(sources))
            await client.run_until_disconnected()
    finally:
        await client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for logger in ("telethon", "aiogram"):
        logging.getLogger(logger).setLevel(logging.CRITICAL)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.error(
            "Stopped: %s. Check config.json, .env and access permissions.",
            type(exc).__name__,
        )
        raise SystemExit(1) from None
