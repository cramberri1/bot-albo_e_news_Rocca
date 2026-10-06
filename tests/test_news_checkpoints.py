"""News must checkpoint completed items before cancellation or the next send."""

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_IDS", "1")
os.environ.pop("GITHUB_ACTIONS", None)

import bot


class NewsCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("DB_PATH", "NEWS_DB_PATH", "USER_SEEN_PATH", "USER_SEEN_NEWS_PATH",
                     "SUBSCRIBERS_PATH", "SUBSCRIBERS_NEWS_PATH", "LAST_CHECK_PATH",
                     "ALBO_SAFETY_PATH", "TELEGRAM_UPDATES_PATH"):
            self.stack.enter_context(patch.object(bot, name, self.root / name))
        self.stack.enter_context(patch.object(bot, "_fernet", None))
        self.stack.enter_context(patch.object(bot, "get_all_news_recipients", return_value={1, 2}))
        self.stack.enter_context(patch.object(bot.asyncio, "sleep", new_callable=AsyncMock))
        self.stack.enter_context(patch.object(bot.httpx, "AsyncClient", side_effect=AssertionError("Unexpected network")))
        self.fetch = self.stack.enter_context(patch.object(bot, "fetch_news_html", new_callable=AsyncMock))
        self.notify = self.stack.enter_context(patch.object(bot, "notify_news", new_callable=AsyncMock))
        self.notify.return_value = ({1, 2}, set())
        self.telegram = AsyncMock()
        self.remote = {}
        self.push = self.stack.enter_context(
            patch.object(bot, "git_commit_and_push", side_effect=self.persist))
        bot.save_news_db({}, push=False)
        bot.save_user_seen_news({}, push=False)
        self.persist([str(bot.NEWS_DB_PATH), str(bot.USER_SEEN_NEWS_PATH)])

    def persist(self, paths=None, **kwargs):
        """A successful remote checkpoint retains only files already on disk."""
        if paths:
            for name in paths:
                path = Path(name)
                self.remote[path.name] = path.read_bytes()
        return True

    def restart_from_remote(self):
        for name, payload in self.remote.items():
            (self.root / name).write_bytes(payload)

    @staticmethod
    def news(number, *, date=None):
        return {
            "id": str(number), "title": f"News {number}", "category": "Avviso",
            "date": date or datetime.now(timezone.utc).strftime("%d-%m-%Y"),
            "url": f"https://example.invalid/novita_{number}.html", "description": "Fixture",
        }

    def remote_archive(self):
        return json.loads(self.remote[bot.NEWS_DB_PATH.name])

    def remote_history(self):
        return json.loads(self.remote[bot.USER_SEEN_NEWS_PATH.name])

    async def test_unsynced_state_blocks_fetch_and_delivery(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.push.side_effect = None
        self.push.return_value = False
        result = await bot._run_check_news(self.telegram)
        self.assertFalse(result["ok"])
        self.fetch.assert_not_awaited()
        self.notify.assert_not_awaited()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    async def test_crash_during_second_news_preserves_first_receipts_for_restart(self):
        first, second = self.news(1), self.news(2)
        self.fetch.return_value = [first, second]
        self.notify.side_effect = [({1}, {2}), asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await bot._run_check_news(self.telegram)
        self.assertEqual(self.remote_history(), {"1": ["1"]})
        self.assertTrue(self.remote_archive()["1"]["delivery_pending"])
        self.assertFalse(self.remote_archive()['2']['notified'])
        self.assertTrue(self.remote_archive()['2']['delivery_pending'])

        self.restart_from_remote()
        self.notify.reset_mock()

        async def deliver(_bot, _item, targets, **kwargs):
            return set(targets), set()

        self.notify.side_effect = deliver
        result = await bot._run_check_news(self.telegram)
        retries = {call.args[1]["id"]: call.args[2] for call in self.notify.await_args_list}
        self.assertEqual(retries, {"1": {2}, "2": {1, 2}})
        self.assertTrue(result["ok"])
        self.assertEqual(self.remote_history(), {"1": ["1", "2"], "2": ["1", "2"]})
        self.assertFalse(any(rec["delivery_pending"] for rec in self.remote_archive().values()))

    async def test_each_news_reaches_remote_before_next_delivery(self):
        self.fetch.return_value = [self.news(1), self.news(2)]

        async def deliver(_bot, item, targets, **kwargs):
            if item["id"] == "2":
                self.assertTrue(self.remote_archive()["1"]["notified"])
                self.assertEqual(self.remote_history(), {"1": ["1"], "2": ["1"]})
            return set(targets), set()

        self.notify.side_effect = deliver
        result = await bot._run_check_news(self.telegram)
        self.assertTrue(result["ok"])
        self.assertEqual(result["new"], 2)
        self.assertEqual(set(self.remote_archive()), {"1", "2"})
        self.assertEqual(self.push.call_count, 5)  # Initial gate, two intents and two final checkpoints.

    async def test_failed_checkpoint_stops_before_second_news(self):
        self.fetch.return_value = [self.news(1), self.news(2)]
        self.push.side_effect = [True, True, False]
        with self.assertRaisesRegex(RuntimeError, "Checkpoint consegna News"):
            await bot._run_check_news(self.telegram)
        self.notify.assert_awaited_once()
        self.assertEqual(bot.load_user_seen_news(), {"1": ["1"], "2": ["1"]})
        self.assertEqual(self.remote_history(), {})
        self.assertEqual(self.remote_archive(), {})

        self.notify.reset_mock()
        self.push.side_effect = None
        self.push.return_value = False
        self.assertFalse((await bot._run_check_news(self.telegram))["ok"])
        self.notify.assert_not_awaited()

    async def test_all_failed_news_is_durable_pending_and_retries_after_restart(self):
        self.fetch.return_value = [self.news(1)]
        self.notify.return_value = (set(), {1, 2})
        result = await bot._run_check_news(self.telegram)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], 2)
        record = self.remote_archive()["1"]
        self.assertFalse(record["notified"])
        self.assertTrue(record["delivery_pending"])
        self.assertEqual(self.remote_history(), {})

        self.restart_from_remote()
        self.fetch.return_value = []  # Pending is independent of current listing/age.
        self.notify.reset_mock()
        self.notify.return_value = ({1, 2}, set())
        with patch.object(bot, "news_is_recent", return_value=False):
            result = await bot._run_check_news(self.telegram)
        self.assertTrue(result["ok"])
        self.assertEqual(self.notify.await_args.args[2], {1, 2})
        self.assertTrue(self.remote_archive()["1"]["notified"])
        self.assertFalse(self.remote_archive()["1"]["delivery_pending"])

    async def test_old_news_stays_silent_and_is_persisted(self):
        self.fetch.return_value = [self.news(1, date="01-01-2015")]
        result = await bot._run_check_news(self.telegram)
        self.notify.assert_not_awaited()
        self.assertTrue(result["ok"])
        self.assertEqual(result["new"], 0)
        self.assertTrue(self.remote_archive()["1"]["notified"])
        self.assertFalse(self.remote_archive()["1"]["delivery_pending"])
        self.assertEqual(self.remote_history(), {})

    async def test_empty_snapshot_leaves_known_state_unchanged(self):
        bot.save_news_db({"old": {"notified": True}}, push=False)
        before = bot.NEWS_DB_PATH.read_bytes()
        self.fetch.return_value = []
        result = await bot._run_check_news(self.telegram)
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(bot.NEWS_DB_PATH.read_bytes(), before)
        self.notify.assert_not_awaited()
        self.push.assert_called_once_with()

    async def test_fetch_failure_does_not_mutate_archive_or_history(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.fetch.return_value = None
        self.assertFalse((await bot._run_check_news(self.telegram))["ok"])
        self.notify.assert_not_awaited()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.push.assert_called_once_with()

    async def test_cancel_between_items_retains_completed_news(self):
        self.fetch.return_value = [self.news(1), self.news(2)]
        with patch.object(bot.asyncio, "sleep", new_callable=AsyncMock,
                          side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await bot._run_check_news(self.telegram)
        self.notify.assert_awaited_once()
        self.assertTrue(self.remote_archive()["1"]["notified"])
        self.assertEqual(self.remote_history(), {"1": ["1"], "2": ["1"]})

    async def test_existing_encrypted_receipts_deduplicate_pending_targets(self):
        from cryptography.fernet import Fernet

        with patch.object(bot, "_fernet", Fernet(Fernet.generate_key())):
            bot.save_user_seen_news({"1": ["1"]}, push=False)
            bot.save_news_db({"1": {**self.news(1), "notified": True,
                                    "delivery_pending": True}}, push=False)
            self.fetch.return_value = []
            self.notify.return_value = ({2}, set())
            result = await bot._run_check_news(self.telegram)
            self.assertTrue(result["ok"])
            self.assertEqual(self.notify.await_args.args[2], {2})
            payload = self.remote[bot.USER_SEEN_NEWS_PATH.name]
            self.assertTrue(payload.startswith(b"gAAAA"))
            self.assertEqual(bot._decrypt_json(payload), {"1": ["1"], "2": ["1"]})


if __name__ == "__main__":
    unittest.main()
