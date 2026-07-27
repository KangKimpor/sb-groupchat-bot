"""Runnable checks for the non-obvious parts of the bot.

    pip install -r requirements.txt
    python3 -m unittest test_bot.py -v

The one that matters is WebhookLoopTest: it pushes several updates through the
Flask route in a single process to prove the shared event loop survives past the
first request. The original build called asyncio.run() per request, which only
fails on the *second* update -- so a single-request test would have passed while
production broke.

Firestore and Gemini are stubbed here. Nothing in this file touches the network,
Google credentials, or Telegram.
"""

import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("LOCAL_UTC_OFFSET_HOURS", "7")

DAILY_LIMITS = {"ask": 20, "summary": 5, "export": 3, "voice": 15}

logged_messages = []
saved_transcripts = []
transcribe_calls = []
group_voice_increments = []


def _install_stubs():
    """Replace firestore_db and gemini_client before bot.py imports them."""
    fake_db = types.ModuleType("firestore_db")
    fake_db.DAILY_LIMITS = DAILY_LIMITS
    fake_db.RETENTION_DAYS = 10
    fake_db.utcnow = lambda: datetime.now(timezone.utc)
    fake_db.is_admin = lambda user_id: False
    fake_db.check_and_increment = lambda group_id, user_id, kind: True
    fake_db.get_usage = lambda group_id, user_id: {
        kind: (0, limit) for kind, limit in DAILY_LIMITS.items()
    }
    fake_db.list_user_groups = lambda user_id: []
    fake_db.resolve_alias = lambda alias: None
    fake_db.get_group = lambda group_id: None
    fake_db.is_member = lambda group_id, user_id: False
    fake_db.register_group = lambda group_id, name: None
    fake_db.add_member = lambda group_id, user_id: None
    fake_db.set_alias = lambda group_id, alias: None
    fake_db.log_message = lambda *a, **k: logged_messages.append((a, k))
    fake_db.save_transcript = lambda g, m, t: saved_transcripts.append((g, m, t))
    fake_db.get_messages_in_range = lambda group_id, start, end: []
    fake_db.cleanup_old_messages = lambda: 0
    fake_db.check_group_voice_limit = lambda group_id: True
    fake_db.increment_group_voice = lambda g: group_voice_increments.append(g)
    sys.modules["firestore_db"] = fake_db

    fake_gemini = types.ModuleType("gemini_client")
    fake_gemini.MODEL = "stub-model"
    fake_gemini.ask = lambda q: f"answer to: {q}"
    fake_gemini.summarize = lambda c, g, p: "stub summary"

    def fake_transcribe(audio_bytes):
        transcribe_calls.append(audio_bytes)
        return "LANGUAGE: Khmer\nTEXT: The rebar delivery arrives Thursday."

    fake_gemini.transcribe_and_translate = fake_transcribe
    sys.modules["gemini_client"] = fake_gemini
    return fake_db, fake_gemini


fake_db, fake_gemini = _install_stubs()

from telegram import User  # noqa: E402
from telegram.ext import ExtBot  # noqa: E402
from telegram.request import HTTPXRequest  # noqa: E402

BOT_USER = User(id=999, first_name="SingBuildGroupChatBot", is_bot=True,
                username="SingBuildGroupChatBot")

sent_messages = []
sent_documents = []


async def fake_get_me(self, *args, **kwargs):
    # The real Bot.get_me() sets self._bot_user as a side effect. Skip that and
    # every later .bot.username access raises "ExtBot is not properly
    # initialized" -- which looks exactly like the event loop bug but isn't.
    self._bot_user = BOT_USER
    return BOT_USER


async def fake_request_initialize(self):
    return None


async def fake_request_shutdown(self):
    return None


async def fake_send_message(self, chat_id, text, *args, **kwargs):
    sent_messages.append((chat_id, text))
    return None


async def fake_send_document(self, chat_id, document, *args, **kwargs):
    sent_documents.append((chat_id, document))
    return None


async def fake_get_chat_member(self, chat_id, user_id, *args, **kwargs):
    member = mock.MagicMock()
    member.status = "administrator"
    return member


get_file_calls = []


async def fake_get_file(self, file_id, *args, **kwargs):
    """Stand in for fetching voice audio back from Telegram by file_id."""
    get_file_calls.append(file_id)

    class _File:
        async def download_as_bytearray(self):
            return bytearray(b"fake-ogg-bytes")

    return _File()


# Started at import time and deliberately never stopped: bot.py calls
# Application.initialize() at module load, so the patches must already be live.
for target, replacement in [
    ("get_me", fake_get_me),
    ("send_message", fake_send_message),
    ("send_document", fake_send_document),
    ("get_chat_member", fake_get_chat_member),
    ("get_file", fake_get_file),
]:
    mock.patch.object(ExtBot, target, replacement).start()
mock.patch.object(HTTPXRequest, "initialize", fake_request_initialize).start()
mock.patch.object(HTTPXRequest, "shutdown", fake_request_shutdown).start()

import bot  # noqa: E402

GROUP_ID = -1001234567890
USER_ID = 42


def tg_bot():
    """The live ExtBot instance, so patched get_file/send_message apply."""
    return bot.tg_app.bot


def make_update(update_id, text, chat_type="group"):
    entities = []
    if text.startswith("/"):
        command = text.split()[0]
        entities = [{"offset": 0, "length": len(command), "type": "bot_command"}]
    chat = (
        {"id": GROUP_ID, "type": chat_type, "title": "Urban Village P2"}
        if chat_type in ("group", "supergroup")
        else {"id": USER_ID, "type": "private"}
    )
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5000 + update_id,
            "date": 1750000000,
            "chat": chat,
            "from": {
                "id": USER_ID,
                "is_bot": False,
                "first_name": "Por",
                "username": "por",
            },
            "text": text,
            "entities": entities,
        },
    }


class WebhookLoopTest(unittest.TestCase):
    """Several sequential webhook requests in one process must all succeed."""

    def setUp(self):
        bot.app.config["TESTING"] = True
        self.client = bot.app.test_client()
        self.url = f"/webhook/{os.environ['WEBHOOK_SECRET']}"
        sent_messages.clear()
        logged_messages.clear()

    def test_sequential_updates_share_one_loop(self):
        payloads = [
            make_update(1, "/help"),
            make_update(2, "/limits"),
            make_update(3, "Concrete pour moved to Friday"),
            make_update(4, "/ask how long does concrete take to cure"),
        ]
        for payload in payloads:
            response = self.client.post(self.url, json=payload)
            self.assertEqual(
                response.status_code,
                200,
                f"update {payload['update_id']} failed: {response.data!r}",
            )

        self.assertGreaterEqual(len(sent_messages), 3, sent_messages)
        self.assertEqual(len(logged_messages), 1, "only the plain text should be logged")

    def test_health_and_cleanup_auth(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/cleanup").status_code, 403)
        self.assertEqual(self.client.get("/cleanup?key=wrong").status_code, 403)
        ok = self.client.get(f"/cleanup?key={os.environ['WEBHOOK_SECRET']}")
        self.assertEqual(ok.status_code, 200)


class TranscriptParsingTest(unittest.TestCase):
    def test_extracts_text_line(self):
        raw = "LANGUAGE: Khmer\nTEXT: Pour the slab on Thursday."
        self.assertEqual(
            bot._extract_transcript_text(raw), "Pour the slab on Thursday."
        )

    def test_keeps_multiline_text(self):
        raw = "LANGUAGE: Thai\nTEXT: line one\nline two"
        self.assertEqual(bot._extract_transcript_text(raw), "line one\nline two")

    def test_falls_back_to_whole_reply_if_format_changes(self):
        # Better to keep unexpected content than to silently drop it.
        self.assertEqual(bot._extract_transcript_text("just the words"), "just the words")

    def test_empty(self):
        self.assertEqual(bot._extract_transcript_text(""), "[empty transcription]")


class ArgParsingTest(unittest.TestCase):
    def test_defaults_to_today_and_no_alias(self):
        self.assertEqual(bot._parse_group_and_period([]), (None, "today"))

    def test_period_only(self):
        self.assertEqual(bot._parse_group_and_period(["week"]), (None, "week"))

    def test_alias_only(self):
        self.assertEqual(bot._parse_group_and_period(["uvp2"]), ("uvp2", "today"))

    def test_alias_and_period(self):
        self.assertEqual(bot._parse_group_and_period(["uvp2", "week"]), ("uvp2", "week"))

    def test_order_insensitive_and_case_insensitive(self):
        self.assertEqual(bot._parse_group_and_period(["WEEK", "UVP2"]), ("uvp2", "week"))


class PeriodRangeTest(unittest.TestCase):
    def test_week_is_seven_days(self):
        start, end, label = bot._period_range("week")
        self.assertAlmostEqual((end - start).total_seconds(), 7 * 86400, delta=5)
        self.assertEqual(label, "the last 7 days")

    def test_today_starts_at_local_midnight_not_utc_midnight(self):
        start, end, label = bot._period_range("today")
        self.assertEqual(label, "today")
        local_start = start + timedelta(hours=7)
        self.assertEqual((local_start.hour, local_start.minute), (0, 0))
        self.assertLessEqual(start, end)


class VoiceLimitTest(unittest.TestCase):
    """The gap in the original build: transcription must consult both the
    per-user and the group-wide voice caps, not just /summary and /export.

    Also covers fetching audio back from Telegram by file_id, since we store no
    audio ourselves.
    """

    def setUp(self):
        transcribe_calls.clear()
        saved_transcripts.clear()
        group_voice_increments.clear()
        get_file_calls.clear()

    def _run(self, message):
        # Driven on the application's own loop, the same one the webhook route
        # uses -- not a throwaway asyncio.run() loop.
        return bot._loop.run_until_complete(
            bot._transcribe_voice_msg(tg_bot(), GROUP_ID, message, USER_ID)
        )

    def test_cached_transcript_is_not_resent_to_gemini(self):
        message = {"message_id": 1, "transcript": "already done", "file_id": "AwACF1"}
        self.assertEqual(self._run(message), "already done")
        self.assertEqual(transcribe_calls, [])
        self.assertEqual(get_file_calls, [], "must not even ask Telegram for audio")

    def test_fetches_from_telegram_then_transcribes_and_caches(self):
        message = {"message_id": 2, "transcript": None, "file_id": "AwACF2"}
        result = self._run(message)
        self.assertEqual(result, "The rebar delivery arrives Thursday.")
        self.assertEqual(get_file_calls, ["AwACF2"])
        self.assertEqual(len(transcribe_calls), 1)
        self.assertEqual(saved_transcripts, [(GROUP_ID, 2, result)])
        self.assertEqual(group_voice_increments, [GROUP_ID])

    def test_missing_file_id_is_reported_not_crashed(self):
        message = {"message_id": 3, "transcript": None, "file_id": None}
        self.assertIn("unavailable", self._run(message))
        self.assertEqual(get_file_calls, [])

    def test_group_backstop_blocks_before_calling_gemini(self):
        with mock.patch.object(fake_db, "check_group_voice_limit", lambda g: False):
            message = {"message_id": 4, "transcript": None, "file_id": "AwACF4"}
            result = self._run(message)
        self.assertIn("limit reached", result)
        self.assertEqual(transcribe_calls, [])
        self.assertEqual(get_file_calls, [], "must not download before checking caps")

    def test_per_user_voice_limit_blocks_before_calling_gemini(self):
        with mock.patch.object(
            fake_db, "check_and_increment", lambda g, u, kind: kind != "voice"
        ):
            message = {"message_id": 5, "transcript": None, "file_id": "AwACF5"}
            result = self._run(message)
        self.assertIn("limit reached", result)
        self.assertEqual(transcribe_calls, [])
        self.assertEqual(get_file_calls, [])

    def test_failed_transcription_does_not_charge_the_group(self):
        def boom(audio):
            raise RuntimeError("gemini down")

        with mock.patch.object(fake_gemini, "transcribe_and_translate", boom):
            message = {"message_id": 6, "transcript": None, "file_id": "AwACF6"}
            result = self._run(message)
        self.assertIn("failed", result)
        self.assertEqual(group_voice_increments, [])

    def test_deleted_voice_message_degrades_gracefully(self):
        """If the sender deleted the note, Telegram stops serving it."""

        async def gone(self, file_id, *args, **kwargs):
            raise RuntimeError("Bad Request: file is temporarily unavailable")

        with mock.patch.object(ExtBot, "get_file", gone):
            message = {"message_id": 7, "transcript": None, "file_id": "AwACF7"}
            result = self._run(message)
        self.assertIn("failed", result)
        self.assertEqual(group_voice_increments, [])


if __name__ == "__main__":
    unittest.main()
