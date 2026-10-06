"""Bootstrap must distinguish manual cache, legacy history, and durable readiness."""

import asyncio
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_IDS", "1")
os.environ.pop("GITHUB_ACTIONS", None)

import bot


class BaselineInitializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("DB_PATH", "NEWS_DB_PATH", "USER_SEEN_PATH", "USER_SEEN_NEWS_PATH",
                     "SUBSCRIBERS_PATH", "SUBSCRIBERS_NEWS_PATH", "LAST_CHECK_PATH",
                     "ALBO_SAFETY_PATH", "TELEGRAM_UPDATES_PATH",
                     "ALBO_BASELINE_PATH", "NEWS_BASELINE_PATH"):
            self.stack.enter_context(patch.object(bot, name, self.root / name))
        self.stack.enter_context(patch.object(bot, "_fernet", None))
        self.stack.enter_context(patch.object(bot, "_BASELINE_CONFIRMATIONS", {}))
        self.stack.enter_context(patch.object(bot, "_ALBO_CHECK_LOCK", asyncio.Lock()))
        self.stack.enter_context(patch.object(bot, "_NEWS_CHECK_LOCK", asyncio.Lock()))
        self.stack.enter_context(patch.object(bot.httpx, "AsyncClient",
                                            side_effect=AssertionError("Unexpected network")))
        self.stack.enter_context(patch.object(bot, "notify", side_effect=AssertionError("Unexpected delivery")))
        self.stack.enter_context(patch.object(bot, "notify_news", side_effect=AssertionError("Unexpected delivery")))
        self.push = self.stack.enter_context(patch.object(bot, "git_commit_and_push", return_value=True))
        self.fetch_albo = self.stack.enter_context(patch.object(bot, "fetch_albo_html", new_callable=AsyncMock))
        self.fetch_news = self.stack.enter_context(patch.object(bot, "fetch_news_html", new_callable=AsyncMock))
        self.fetch_albo.return_value = [self.item(1)]
        self.fetch_news.return_value = [self.news(1)]

    @staticmethod
    def item(number):
        return dict(title=f"Fixture act {number}", num_pub=str(number), tipo="Determina",
                    sender="Comune", date=datetime.now(timezone.utc).strftime("%d-%m-%Y"))

    @staticmethod
    def news(number):
        return dict(id=str(number), title=f"Fixture news {number}", category="Avviso",
                    date=datetime.now(timezone.utc).strftime("%d-%m-%Y"),
                    url=f"https://example.invalid/novita_{number}.html", description="Fixture")

    def sources(self):
        return (
            ("Albo", bot._ensure_albo_baseline, bot.DB_PATH, bot.ALBO_BASELINE_PATH, self.fetch_albo),
            ("News", bot._ensure_news_baseline, bot.NEWS_DB_PATH, bot.NEWS_BASELINE_PATH, self.fetch_news),
        )

    def clear_source(self, database, marker):
        database.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        bot._BASELINE_CONFIRMATIONS.clear()
        self.push.reset_mock()
        self.push.side_effect = None
        self.push.return_value = True

    async def test_manual_cache_before_bootstrap_becomes_silent_baseline_without_history_reset(self):
        items = [self.item(number) for number in range(20)]
        cached = {bot.item_id(item): dict(item, notified=False) for item in items}
        cached["not-current"] = dict(title="Cached historical act", notified=False)
        bot.save_db(cached, push=False)
        bot.save_user_seen({"2": ["historical-receipt"]}, push=False)
        receipts = bot.USER_SEEN_PATH.read_bytes()
        self.fetch_albo.return_value = items

        self.assertTrue(await bot._ensure_albo_baseline())
        database = bot.load_db()
        self.assertTrue(all(database[bot.item_id(item)]["notified"] for item in items))
        self.assertEqual(database["not-current"], cached["not-current"])
        self.assertEqual(bot.USER_SEEN_PATH.read_bytes(), receipts)
        self.assertTrue(json.loads(bot.ALBO_BASELINE_PATH.read_text())["initialized"])
        self.assertIn(str(bot.ALBO_BASELINE_PATH), self.push.call_args.args[0])

    async def test_stale_identity_safety_flag_does_not_make_manual_cache_a_baseline(self):
        item = self.item(1)
        bot.save_db({bot.item_id(item): dict(item, notified=False)}, push=False)
        bot.ALBO_SAFETY_PATH.write_text('{"identity_v2_ready": true}', encoding="utf-8")
        original_safety = bot.ALBO_SAFETY_PATH.read_bytes()
        self.assertTrue(await bot._ensure_albo_baseline())
        self.fetch_albo.assert_awaited_once()
        self.assertTrue(bot.load_db()[bot.item_id(item)]["notified"])
        self.assertEqual(bot.ALBO_SAFETY_PATH.read_bytes(), original_safety)

    async def test_bootstrap_preserves_canonical_key_aliases_and_user_receipts(self):
        item = self.item(1)
        record = dict(item, notified=False)
        bot.remember_identity(record, item)
        record["legacy_ids"].append("existing-alias")
        bot.save_db({"existing-canonical-key": record}, push=False)
        bot.save_user_seen({"2": ["existing-canonical-key", "existing-alias"]}, push=False)
        receipts = bot.USER_SEEN_PATH.read_bytes()

        self.assertTrue(await bot._ensure_albo_baseline())
        database = bot.load_db()
        self.assertEqual(set(database), {"existing-canonical-key"})
        self.assertIn("existing-alias", database["existing-canonical-key"]["legacy_ids"])
        self.assertTrue(database["existing-canonical-key"]["notified"])
        self.assertEqual(bot.USER_SEEN_PATH.read_bytes(), receipts)

    async def test_initialized_legacy_albo_archive_is_never_refetched_or_rewritten(self):
        for payload in ({"historical": {"notified": True}}, {"historical": {"date": "01-01-2024"}},
                        ["historical"], {}, []):
            with self.subTest(payload=payload):
                bot.DB_PATH.write_text(json.dumps(payload), encoding="utf-8")
                before = bot.DB_PATH.read_bytes()
                self.assertTrue(await bot._ensure_albo_baseline())
                self.assertEqual(bot.DB_PATH.read_bytes(), before)
        self.fetch_albo.assert_not_awaited()
        self.push.assert_not_called()
        self.assertFalse(bot.ALBO_BASELINE_PATH.exists())

    async def test_all_failed_global_deliveries_are_preserved_instead_of_rebaselined(self):
        for global_fields in ({"delivery_pending": True}, {"delivery_pending": False},
                              {"revision_fingerprint": "previous-version"},
                              {"baseline_reason": "historical_or_unknown_date"}):
            with self.subTest(fields=global_fields):
                record = dict(self.item(1), notified=False, **global_fields)
                bot.save_db({"original": record}, push=False)
                before = bot.DB_PATH.read_bytes()
                self.assertTrue(await bot._ensure_albo_baseline())
                self.assertEqual(bot.DB_PATH.read_bytes(), before)
        self.fetch_albo.assert_not_awaited()
        self.push.assert_not_called()

    async def test_initialized_news_archives_include_empty_and_pending_without_refetch(self):
        for payload in ({}, {"historical": {"notified": True}},
                        {"pending": {"notified": False, "delivery_pending": True}}):
            with self.subTest(payload=payload):
                bot.save_news_db(payload, push=False)
                before = bot.NEWS_DB_PATH.read_bytes()
                self.assertTrue(await bot._ensure_news_baseline())
                self.assertEqual(bot.NEWS_DB_PATH.read_bytes(), before)
        self.fetch_news.assert_not_awaited()
        self.push.assert_not_called()

    async def test_corrupt_databases_fail_closed_without_fetch_or_reset(self):
        for name, ensure, database, marker, fetch in self.sources():
            with self.subTest(source=name):
                database.write_text("{broken", encoding="utf-8")
                before = database.read_bytes()
                with self.assertRaises(json.JSONDecodeError):
                    await ensure()
                self.assertEqual(database.read_bytes(), before)
                self.assertFalse(marker.exists())
                fetch.assert_not_awaited()
        self.push.assert_not_called()

    async def test_unexpected_database_formats_fail_closed_without_reset(self):
        for name, ensure, database, marker, fetch in self.sources():
            payloads = (None, 12, "unexpected") if name == "Albo" else (None, [], {"invalid": None})
            for payload in payloads:
                with self.subTest(source=name, payload=payload):
                    database.write_text(json.dumps(payload), encoding="utf-8")
                    before = database.read_bytes()
                    with self.assertRaises(RuntimeError):
                        await ensure()
                    self.assertEqual(database.read_bytes(), before)
                    self.assertFalse(marker.exists())
                    fetch.assert_not_awaited()
        self.push.assert_not_called()

    async def test_unavailable_source_leaves_manual_cache_and_receipts_unchanged(self):
        record = dict(self.item(1), notified=False)
        bot.save_db({"manual": record}, push=False)
        bot.save_user_seen({"2": ["manual"]}, push=False)
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.fetch_albo.return_value = None
        self.assertFalse(await bot._ensure_albo_baseline())
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})
        self.assertFalse(bot.ALBO_BASELINE_PATH.exists())
        self.push.assert_not_called()

    async def test_partial_albo_snapshot_cannot_certify_a_baseline(self):
        record = dict(self.item(1), notified=False)
        bot.save_db({"manual": record}, push=False)
        before = bot.DB_PATH.read_bytes()
        for flag, value in (("incomplete_pagination", [2]), ("unreadable_pages", [2]), ("partial_pages", [2]),
                            ("skipped_cards", 1)):
            with self.subTest(flag=flag):
                async def incomplete(**kwargs):
                    kwargs["diagnostics"][flag] = value
                    return [self.item(1)]

                self.fetch_albo.side_effect = incomplete
                self.assertFalse(await bot._ensure_albo_baseline())
                self.assertEqual(bot.DB_PATH.read_bytes(), before)
                self.assertFalse(bot.ALBO_BASELINE_PATH.exists())
        self.push.assert_not_called()

    async def test_empty_new_baseline_is_persisted_explicitly_and_not_repeated(self):
        for name, ensure, database, marker, fetch in self.sources():
            with self.subTest(source=name):
                fetch.return_value = []
                self.assertTrue(await ensure())
                self.assertEqual(json.loads(database.read_text()), {})
                self.assertTrue(json.loads(marker.read_text())["initialized"])
                fetch.assert_awaited_once()
                push_count = self.push.call_count
                self.assertTrue(await ensure())
                self.assertEqual(self.push.call_count, push_count)
                fetch.assert_awaited_once()

    async def test_failed_bootstrap_push_keeps_snapshot_pending_and_retries_without_refetch(self):
        for name, ensure, database, marker, fetch in self.sources():
            with self.subTest(source=name):
                self.clear_source(database, marker)
                self.push.return_value = False
                self.assertFalse(await ensure())
                self.assertTrue(database.exists())
                self.assertTrue(marker.exists())
                original_database, original_marker = database.read_bytes(), marker.read_bytes()
                self.assertFalse(await ensure())
                fetch.assert_awaited_once()
                self.assertEqual(database.read_bytes(), original_database)
                self.assertEqual(marker.read_bytes(), original_marker)

                self.push.return_value = True
                self.assertTrue(await ensure())
                fetch.assert_awaited_once()
                paths = self.push.call_args.args[0]
                self.assertIn(str(database), paths)
                self.assertIn(str(marker), paths)
                confirmed_pushes = self.push.call_count
                self.assertTrue(await ensure())
                self.assertEqual(self.push.call_count, confirmed_pushes)

    async def test_restart_reconfirms_existing_marker_before_readiness(self):
        for name, ensure, database, marker, fetch in self.sources():
            with self.subTest(source=name):
                self.assertTrue(await ensure())
                before = database.read_bytes()
                bot._BASELINE_CONFIRMATIONS.clear()  # A new runner/process.
                self.push.return_value = False
                self.assertFalse(await ensure())
                self.assertEqual(database.read_bytes(), before)
                self.push.return_value = True
                self.assertTrue(await ensure())
                fetch.assert_awaited_once()

    async def test_retry_does_not_swallow_items_appearing_after_initial_snapshot(self):
        self.push.return_value = False
        self.assertFalse(await bot._ensure_news_baseline())
        self.fetch_news.return_value = [self.news(1), self.news(2)]
        self.push.return_value = True
        self.assertTrue(await bot._ensure_news_baseline())
        self.fetch_news.assert_awaited_once()
        self.assertEqual(set(bot.load_news_db()), {"1"})

    async def test_missing_database_invalidates_confirmation_and_rebuilds_without_notifications(self):
        for name, ensure, database, marker, fetch in self.sources():
            with self.subTest(source=name):
                self.assertTrue(await ensure())
                generation = json.loads(marker.read_text())["generation"]
                database.unlink()
                self.push.return_value = False
                self.assertFalse(await ensure())
                self.assertEqual(fetch.await_count, 2)
                self.assertNotEqual(json.loads(marker.read_text())["generation"], generation)
                self.push.return_value = True

    async def test_confirmed_empty_baseline_does_not_silence_later_manual_cache(self):
        self.fetch_albo.return_value = []
        self.assertTrue(await bot._ensure_albo_baseline())
        item = self.item(2)
        bot.save_db({bot.item_id(item): dict(item, notified=False)}, push=False)
        before = bot.DB_PATH.read_bytes()
        self.assertTrue(await bot._ensure_albo_baseline())
        self.assertEqual(bot.DB_PATH.read_bytes(), before)
        self.assertFalse(bot.load_db()[bot.item_id(item)]["notified"])
        self.fetch_albo.assert_awaited_once()

    async def test_marker_change_requires_fresh_confirmation_and_corruption_never_resets(self):
        self.assertTrue(await bot._ensure_news_baseline())
        marker = json.loads(bot.NEWS_BASELINE_PATH.read_text())
        marker["generation"] = "different-bootstrap"
        bot.NEWS_BASELINE_PATH.write_text(json.dumps(marker), encoding="utf-8")
        self.push.return_value = False
        self.assertFalse(await bot._ensure_news_baseline())
        self.assertEqual(self.push.call_count, 2)
        for payload in ("{broken", '{"version": true, "initialized": true}',
                        '{"version": 1, "initialized": false}', '[]'):
            with self.subTest(payload=payload):
                bot.NEWS_BASELINE_PATH.write_text(payload, encoding="utf-8")
                before = bot.NEWS_DB_PATH.read_bytes()
                with self.assertRaises((RuntimeError, json.JSONDecodeError)):
                    await bot._ensure_news_baseline()
                self.assertEqual(bot.NEWS_DB_PATH.read_bytes(), before)
        self.fetch_news.assert_awaited_once()

    async def test_confirmed_marker_does_not_hide_a_corrupt_database(self):
        self.assertTrue(await bot._ensure_albo_baseline())
        bot.DB_PATH.write_text("{broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            await bot._ensure_albo_baseline()
        self.fetch_albo.assert_awaited_once()
        self.assertEqual(self.push.call_count, 1)

    async def test_archive_savers_report_failed_push_and_preserve_local_changes(self):
        self.push.return_value = False
        operations = (
            (lambda: bot.save_db({"cached": {"notified": False}}), bot.DB_PATH, {"cached"}),
            (lambda: bot.save_seen({"baseline"}), bot.DB_PATH, {"cached", "baseline"}),
            (lambda: bot.save_news_db({"old": {"notified": True}}), bot.NEWS_DB_PATH, {"old"}),
            (lambda: bot.save_seen_news({"news"}, {"news": self.news(1)}), bot.NEWS_DB_PATH, {"news"}),
        )
        for save, path, expected_keys in operations:
            with self.subTest(path=path, keys=expected_keys):
                self.assertFalse(save())
                self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))), expected_keys)

    async def test_bootstrap_timestamp_is_part_of_the_same_checkpoint_as_the_baseline(self):
        self.push.return_value = False
        self.assertFalse(await bot._ensure_albo_baseline())
        self.push.assert_called_once()
        self.assertEqual(set(self.push.call_args.args[0]), {
            str(bot.DB_PATH), str(bot.ALBO_BASELINE_PATH), str(bot.LAST_CHECK_PATH),
        })

    async def test_manual_check_requires_silent_albo_baseline_on_cold_start_or_manual_only_cache(self):
        items = [self.item(number) for number in range(20)]
        self.fetch_albo.return_value = items
        request = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=1), message=AsyncMock(),
        )
        context = SimpleNamespace(bot=AsyncMock())

        async def assert_initialized(_bot):
            self.assertTrue(bot.ALBO_BASELINE_PATH.exists())
            self.assertTrue(all(bot.load_db()[bot.item_id(item)]["notified"] for item in items))
            self.fetch_albo.assert_awaited_once()
            return {"ok": True, "new": 0, "total": len(items)}

        for cached in (False, True):
            with self.subTest(manual_cache=cached):
                self.clear_source(bot.DB_PATH, bot.ALBO_BASELINE_PATH)
                self.fetch_albo.reset_mock()
                if cached:
                    bot.save_db({bot.item_id(item): dict(item, notified=False) for item in items}, push=False)
                # A flag from a previous DB must not certify the replacement.
                bot.ALBO_SAFETY_PATH.write_text('{"identity_v2_ready": true}', encoding="utf-8")
                with patch.dict(bot.CONFIG, {"ADMIN_IDS": [1]}), \
                        patch.object(bot, "run_check", new_callable=AsyncMock,
                                     side_effect=assert_initialized) as check, \
                        patch.object(bot, "run_check_news", new_callable=AsyncMock,
                                     return_value={"ok": True}):
                    await bot.cmd_controlla(request, context)
                check.assert_awaited_once()

    async def test_manual_check_blocks_albo_delivery_after_failed_bootstrap_but_checks_news(self):
        request = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=1), message=AsyncMock(),
        )
        self.fetch_albo.return_value = None
        with patch.dict(bot.CONFIG, {"ADMIN_IDS": [1]}), \
                patch.object(bot, "run_check", new_callable=AsyncMock) as check_albo, \
                patch.object(bot, "run_check_news", new_callable=AsyncMock,
                             return_value={"ok": True}) as check_news:
            await bot.cmd_controlla(request, SimpleNamespace(bot=AsyncMock()))
        check_albo.assert_not_awaited()
        check_news.assert_awaited_once()
        self.assertFalse(bot.ALBO_BASELINE_PATH.exists())
        response = request.message.reply_text.await_args.args[0]
        self.assertIn("❌ Albo", response)
        self.assertIn("✅ News", response)

    async def test_manual_check_preserves_corrupt_albo_and_reports_independent_news_result(self):
        request = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=1), message=AsyncMock(),
        )
        bot.DB_PATH.write_text("{broken", encoding="utf-8")
        with patch.dict(bot.CONFIG, {"ADMIN_IDS": [1]}), \
                patch.object(bot, "run_check", new_callable=AsyncMock) as check_albo, \
                patch.object(bot, "run_check_news", new_callable=AsyncMock,
                             return_value={"ok": True}) as check_news:
            await bot.cmd_controlla(request, SimpleNamespace(bot=AsyncMock()))
        check_albo.assert_not_awaited()
        check_news.assert_awaited_once()
        self.assertEqual(bot.DB_PATH.read_text(encoding="utf-8"), "{broken")
        self.fetch_albo.assert_not_awaited()
        self.assertIn("❌ Albo", request.message.reply_text.await_args.args[0])
        self.assertIn("✅ News", request.message.reply_text.await_args.args[0])

    async def test_concurrent_bootstrap_requests_share_one_snapshot_and_checkpoint(self):
        for name, ensure, database, marker, fetch in self.sources():
            with self.subTest(source=name):
                self.clear_source(database, marker)
                started, release = asyncio.Event(), asyncio.Event()
                result = [self.item(1)] if name == "Albo" else [self.news(1)]

                async def slow_fetch(*_args, **_kwargs):
                    started.set()
                    await release.wait()
                    return copy.deepcopy(result)

                fetch.side_effect = slow_fetch
                first = asyncio.create_task(ensure())
                second = None
                try:
                    await started.wait()
                    second = asyncio.create_task(ensure())
                    await asyncio.sleep(0)
                    self.assertFalse(second.done())
                    fetch.assert_awaited_once()
                    release.set()
                    self.assertEqual(await asyncio.gather(first, second), [True, True])
                    self.assertEqual(self.push.call_count, 1)
                    fetch.assert_awaited_once()
                finally:
                    release.set()
                    for task in (first, second):
                        if task is not None and not task.done():
                            task.cancel()
                    await asyncio.gather(*(task for task in (first, second) if task is not None),
                                         return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
