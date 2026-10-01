"""Malformed-state and authorization boundaries for the private status report."""

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_IDS", "1")
os.environ.pop("GITHUB_ACTIONS", None)

import bot
from admin_status import CheckHealth


class AdminStatusEdgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("DB_PATH", "NEWS_DB_PATH", "SUBSCRIBERS_PATH",
                     "SUBSCRIBERS_NEWS_PATH", "ALBO_SAFETY_PATH", "LAST_CHECK_PATH"):
            self.stack.enter_context(patch.object(bot, name, self.root / name))
        self.stack.enter_context(patch.object(bot, "_fernet", None))
        self.stack.enter_context(patch.object(bot, "_CHECK_HEALTH", CheckHealth()))
        self.stack.enter_context(patch.dict(bot.CONFIG, ADMIN_IDS=[1, 2]))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.write(bot.DB_PATH, {"known": {"notified": True}})
        self.write(bot.NEWS_DB_PATH, {"known": {}})
        self.write(bot.SUBSCRIBERS_PATH, [1, 3])
        self.write(bot.SUBSCRIBERS_NEWS_PATH, [3])
        self.write(bot.ALBO_SAFETY_PATH, {"identity_v2_ready": True})

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_subscriber_schema_errors_never_become_zero_or_partial_counts(self):
        malformed = ({"sensitive-token": 1}, "sensitive-token", None, 2,
                     [True], [0], ["123456789"], [None], [[1]])
        for value in malformed:
            with self.subTest(value=value):
                self.write(bot.SUBSCRIBERS_PATH, value)
                text = bot.build_admin_status()
                self.assertIn("Iscrizioni: dati non disponibili", text)
                self.assertNotIn("Chat iscritte uniche:", text)
                self.assertNotIn("sensitive-token", text)
                self.assertNotIn("123456789", text)
                self.assertIn("Atti conservati: 1", text)
                self.assertIn("News conservate: 1", text)

    def test_archive_schema_errors_are_isolated_and_not_legacy_baselines(self):
        malformed = (None, "sensitive-token", 12, {"bad": None},
                     {"bad": []}, {"bad": {"notified": "false"}},
                     {"bad": {"delivery_pending": 1}})
        for name, label, other in (("DB_PATH", "Albo", "News conservate: 1"),
                                   ("NEWS_DB_PATH", "News", "Atti conservati: 1")):
            path = getattr(bot, name)
            for value in malformed:
                with self.subTest(archive=label, value=value):
                    self.write(path, value)
                    text = bot.build_admin_status()
                    self.assertIn(f"Archivio {label}: dati non disponibili", text)
                    self.assertIn(other, text)
                    self.assertNotIn("sensitive-token", text)
            self.write(path, {"known": {"notified": True}})

    def test_missing_archives_are_unknown_not_empty_successful_baselines(self):
        bot.DB_PATH.unlink()
        bot.NEWS_DB_PATH.unlink()
        text = bot.build_admin_status()
        self.assertIn("Archivio Albo: dati non disponibili", text)
        self.assertIn("Archivio News: dati non disponibili", text)
        self.assertNotIn("Atti conservati: 0", text)
        self.assertNotIn("News conservate: 0", text)

    def test_negative_group_destinations_are_deduplicated_without_disclosure(self):
        group = -1001234567890
        self.write(bot.SUBSCRIBERS_PATH, [1, group, group])
        self.write(bot.SUBSCRIBERS_NEWS_PATH, [group, 5])
        with patch.dict(bot.CONFIG, ADMIN_IDS=[1, 2, group]):
            text = bot.build_admin_status()
        for expected in ("Albo: 2", "News: 2", "Chat iscritte uniche: 3",
                         "Entrambe: 1", "Destinatari Albo: 3",
                         "Destinatari News: 4", "Destinatari unici: 4"):
            self.assertIn(expected, text)
        self.assertNotIn(str(group), text)

    def test_untrusted_persisted_diagnostics_are_bounded_and_never_echoed(self):
        secret = "sensitive-token-" * 400
        bot.LAST_CHECK_PATH.write_text(secret, encoding="utf-8")
        self.write(bot.ALBO_SAFETY_PATH, {"identity_v2_ready": True, "last_stop": secret})
        text = bot.build_admin_status()
        self.assertIn("Ultimo tentativo Albo salvato: non disponibile", text)
        self.assertIn("Blocco Albo registrato: sì", text)
        self.assertNotIn("sensitive-token", text)
        self.assertLess(len(text), 4096)
        self.write(bot.ALBO_SAFETY_PATH, {"identity_v2_ready": "false"})
        self.assertIn("Protezioni Albo: stato salvato non disponibile", bot.build_admin_status())

    def test_private_auth_requires_real_positive_matching_ids(self):
        def request(chat=1, user=1, kind="private"):
            return SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat, type=kind),
                effective_user=SimpleNamespace(id=user))

        self.assertTrue(bot.is_admin_request(request()))
        denied = [request(True, 1), request(1, True), request("1", 1),
                  request(1, "1"), request(-10, -10), request(0, 0),
                  request(1, 1, "group"), request(1, 1, "channel"),
                  SimpleNamespace(effective_chat=None, effective_user=None),
                  SimpleNamespace()]
        with patch.dict(bot.CONFIG, ADMIN_IDS=[1, -10, 0]):
            for update in denied:
                with self.subTest(update=update):
                    self.assertFalse(bot.is_admin_request(update))

    async def test_status_does_not_wait_for_check_locks_or_touch_persisted_state(self):
        lock = asyncio.Lock()
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        request = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.object(bot, "_ALBO_CHECK_LOCK", lock), \
                patch.object(bot, "git_commit_and_push") as persist, \
                patch.object(bot, "run_check", new_callable=AsyncMock) as check, \
                patch.object(bot, "run_check_news", new_callable=AsyncMock) as news_check, \
                patch.object(bot, "_atomic_write_text") as write_text, \
                patch.object(bot, "_atomic_write_bytes") as write_bytes:
            async with lock:
                await asyncio.wait_for(bot.cmd_status(request, SimpleNamespace(bot=None)), timeout=1)
            persist.assert_not_called()
            check.assert_not_awaited()
            news_check.assert_not_awaited()
            write_text.assert_not_called()
            write_bytes.assert_not_called()
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})
        request.message.reply_text.assert_awaited_once()


class CheckHealthEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_result_is_not_success_and_counters_are_not_echoed(self):
        for result in (None, "sensitive-token", {"ok": "true", "failed": "sensitive-token"},
                       {"ok": 1, "new": -1, "failed": True, "updated": 2**128}):
            with self.subTest(result=result):
                monitor = CheckHealth()
                self.assertIs(await monitor.run("Albo", AsyncMock(return_value=result), None), result)
                text = "\n".join(monitor.lines())
                self.assertIn("Albo: incompleto o bloccato", text)
                self.assertIsNone(monitor.checks["Albo"]["last_success"])
                self.assertNotIn("sensitive-token", text)
                self.assertNotIn(str(2**128), text)

    async def test_error_after_success_preserves_time_but_clears_previous_counters(self):
        monitor = CheckHealth()
        await monitor.run("Albo", AsyncMock(return_value={"ok": True, "new": 7, "failed": 0}), None)
        succeeded = monitor.checks["Albo"]["last_success"]
        with self.assertRaises(RuntimeError):
            await monitor.run("Albo", AsyncMock(side_effect=RuntimeError("sensitive-token")), None)
        self.assertEqual(monitor.checks["Albo"]["last_success"], succeeded)
        self.assertIsNone(monitor.checks["Albo"]["new"])
        self.assertIsNone(monitor.checks["Albo"]["failed"])
        text = "\n".join(monitor.lines())
        self.assertIn("Albo: errore", text)
        self.assertNotIn("sensitive-token", text)


if __name__ == "__main__":
    unittest.main()
