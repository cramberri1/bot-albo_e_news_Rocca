"""Commands must preserve receipts and avoid premature persistence confirmations."""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_IDS", "1")
os.environ.pop("GITHUB_ACTIONS", None)

import bot


class StateCommandRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("DB_PATH", "NEWS_DB_PATH", "USER_SEEN_PATH", "USER_SEEN_NEWS_PATH",
                     "SUBSCRIBERS_PATH", "SUBSCRIBERS_NEWS_PATH", "LAST_CHECK_PATH",
                     "ALBO_SAFETY_PATH", "TELEGRAM_UPDATES_PATH"):
            self.stack.enter_context(patch.object(bot, name, self.root / name))
        self.stack.enter_context(patch.object(bot, "_fernet", None))
        self.stack.enter_context(patch.dict(bot.CONFIG, {"ADMIN_IDS": [1]}))
        self.stack.enter_context(patch.object(bot.asyncio, "sleep", new_callable=AsyncMock))
        self.stack.enter_context(patch.object(bot.httpx, "AsyncClient",
                                            side_effect=AssertionError("Unexpected network")))
        self.push = self.stack.enter_context(patch.object(bot, "git_commit_and_push", return_value=True))

    @staticmethod
    def update(chat_id=2):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, type="private"),
            effective_user=SimpleNamespace(id=chat_id),
            message=AsyncMock(),
        )

    def seed_subscriptions(self, subscribed=False):
        for path in (bot.SUBSCRIBERS_PATH, bot.SUBSCRIBERS_NEWS_PATH):
            bot._save_set(path, {2} if subscribed else set())

    def assert_pending_response(self, update):
        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.await_args.args[0]
        self.assertTrue(text.startswith("⚠️"))
        self.assertIn("salvataggio persistente", text)
        self.assertIn("Riprova", text)

    async def test_all_subscription_commands_stop_before_mutation_if_sync_fails(self):
        for name in ("cmd_abbonati", "cmd_disabbonati", "cmd_abbonati_albo",
                     "cmd_disabbonati_albo", "cmd_abbonati_news", "cmd_disabbonati_news"):
            with self.subTest(command=name):
                self.seed_subscriptions(subscribed="disabbonati" in name)
                before = {path.name: path.read_bytes() for path in
                          (bot.SUBSCRIBERS_PATH, bot.SUBSCRIBERS_NEWS_PATH)}
                self.push.side_effect = None
                self.push.return_value = False
                update = self.update()
                await getattr(bot, name)(update, None)
                self.assert_pending_response(update)
                self.assertEqual(before, {path.name: path.read_bytes() for path in
                                          (bot.SUBSCRIBERS_PATH, bot.SUBSCRIBERS_NEWS_PATH)})

    async def test_failed_subscription_push_keeps_local_change_without_success_reply(self):
        for name in ("cmd_abbonati", "cmd_disabbonati", "cmd_abbonati_albo",
                     "cmd_disabbonati_albo", "cmd_abbonati_news", "cmd_disabbonati_news"):
            with self.subTest(command=name):
                removing = "disabbonati" in name
                self.seed_subscriptions(subscribed=removing)
                self.push.side_effect = [True, False]  # Entry sync, then mutation checkpoint.
                update = self.update()
                await getattr(bot, name)(update, None)
                self.assert_pending_response(update)
                path = bot.SUBSCRIBERS_NEWS_PATH if name.endswith("_news") else bot.SUBSCRIBERS_PATH
                self.assertEqual(bot._load_set(path), set() if removing else {2})

    async def test_noop_subscription_retry_requires_sync_before_confirming_local_state(self):
        self.seed_subscriptions()
        self.push.side_effect = [True, False, False, True]
        first, blocked_retry, confirmed_retry = self.update(), self.update(), self.update()
        await bot.cmd_abbonati_news(first, None)
        self.assert_pending_response(first)
        self.assertEqual(bot.load_subscribers_news(), {2})

        await bot.cmd_abbonati_news(blocked_retry, None)
        self.assert_pending_response(blocked_retry)
        await bot.cmd_abbonati_news(confirmed_retry, None)
        self.assertIn("Sei già iscritto", confirmed_retry.message.reply_text.await_args.args[0])
        self.assertEqual(self.push.call_count, 4)

    async def test_partial_combined_subscription_checkpoint_is_not_reported_as_completed(self):
        self.seed_subscriptions()
        self.push.side_effect = [True, True, False]
        update = self.update()
        await bot.cmd_abbonati(update, None)
        self.assert_pending_response(update)
        self.assertEqual(bot.load_subscribers(), {2})
        self.assertEqual(bot.load_subscribers_news(), {2})

    async def test_mark_user_seen_reports_failed_push_and_keeps_local_receipt(self):
        self.push.return_value = False
        self.assertFalse(bot.mark_user_seen(2, ["fixture"]))
        self.assertEqual(bot.load_user_seen(), {"2": ["fixture"]})

    @staticmethod
    def items():
        today = datetime.now(timezone.utc)
        return [
            dict(title=f"Fixture {number}", num_pub=str(number),
                 date=today.strftime("%d-%m-%Y"),
                 date_end=(today + timedelta(days=3)).strftime("%d-%m-%Y"),
                 allegati=[dict(filename="fixture.pdf", content=b"%PDF-1.4 fixture")])
            for number in (1, 2)
        ]

    async def test_manual_delivery_checkpoint_survives_cancellation_during_next_item(self):
        items = self.items()
        remote_receipts = None

        def persist(paths=None, **_kwargs):
            nonlocal remote_receipts
            if paths and str(bot.USER_SEEN_PATH) in paths:
                remote_receipts = bot.USER_SEEN_PATH.read_bytes()
            return True

        self.push.side_effect = persist
        with patch.object(bot, "fetch_albo_html", new_callable=AsyncMock, return_value=items), \
                patch.object(bot, "reply_item", new_callable=AsyncMock,
                             side_effect=[True, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await bot.cmd_atti(self.update(), None)
        self.assertEqual(json.loads(remote_receipts), {"2": [bot.item_id(items[0])]})

    async def test_manual_delivery_stops_after_failed_checkpoint_preserving_local_receipt(self):
        items = self.items()
        spools = []
        payload = b"%PDF-1.4 fixture"
        for item in items:
            with tempfile.NamedTemporaryFile(dir=bot._ATTACHMENT_TEMP_DIR, delete=False) as handle:
                handle.write(payload)
                path = Path(handle.name)
            self.assertTrue(bot._reserve_attachment_spool(len(payload)))
            spool = bot._AttachmentSpool(path, len(payload), hashlib.sha256(payload).hexdigest())
            self.addCleanup(spool.cleanup)
            spools.append(spool)
            item["allegati"] = [dict(filename="fixture.pdf", _spool=spool)]
        self.push.return_value = False
        update = self.update()
        with patch.object(bot, "fetch_albo_html", new_callable=AsyncMock, return_value=items), \
                patch.object(bot, "reply_item", new_callable=AsyncMock, return_value=True) as deliver:
            await bot.cmd_atti(update, None)
        deliver.assert_awaited_once()
        self.assertEqual(bot.load_user_seen(), {"2": [bot.item_id(items[0])]})
        response = update.message.reply_text.await_args.args[0]
        self.assertIn("salvataggio persistente", response)
        self.assertIn("altri invii sono sospesi", response)
        self.assertTrue(all(not spool.path.exists() for spool in spools))

    async def test_failed_manual_delivery_is_not_recorded_as_success(self):
        items = self.items()
        with patch.object(bot, "fetch_albo_html", new_callable=AsyncMock, return_value=items), \
                patch.object(bot, "reply_item", new_callable=AsyncMock, side_effect=[False, True]):
            await bot.cmd_atti(self.update(), None)
        self.assertEqual(bot.load_user_seen(), {"2": [bot.item_id(items[1])]})

    @staticmethod
    def news():
        return dict(id="fixture", title="Fixture News", category="Avviso",
                    date=datetime.now(timezone.utc).strftime("%d-%m-%Y"),
                    url="https://example.invalid/novita_fixture.html", description="Fixture")

    async def test_first_manual_check_initializes_news_without_historical_notifications(self):
        news = self.news()
        context = SimpleNamespace(bot=AsyncMock())
        with patch.object(bot, "run_check", new_callable=AsyncMock, return_value={"ok": True}), \
                patch.object(bot, "run_check_news", side_effect=bot._run_check_news), \
                patch.object(bot, "fetch_news_html", new_callable=AsyncMock, return_value=[news]) as fetch, \
                patch.object(bot, "notify_news", new_callable=AsyncMock) as notify:
            await bot.cmd_controlla(self.update(1), context)
        notify.assert_not_awaited()
        self.assertEqual(fetch.await_count, 2)  # Baseline acquisition, then ordinary check.
        self.assertTrue(bot.load_news_db()["fixture"]["notified"])

    async def test_manual_check_existing_news_archive_still_notifies_real_new_items(self):
        bot.save_news_db({"old": {"notified": True}}, push=False)
        context = SimpleNamespace(bot=AsyncMock())
        with patch.object(bot, "run_check", new_callable=AsyncMock, return_value={"ok": True}), \
                patch.object(bot, "run_check_news", side_effect=bot._run_check_news), \
                patch.object(bot, "fetch_news_html", new_callable=AsyncMock,
                             return_value=[self.news()]) as fetch, \
                patch.object(bot, "get_all_news_recipients", return_value={1}), \
                patch.object(bot, "notify_news", new_callable=AsyncMock,
                             return_value=({1}, set())) as notify:
            await bot.cmd_controlla(self.update(1), context)
        notify.assert_awaited_once()
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(bot.load_user_seen_news(), {"1": ["fixture"]})

    async def test_manual_check_failed_news_bootstrap_does_not_run_news_delivery(self):
        context = SimpleNamespace(bot=AsyncMock())
        with patch.object(bot, "run_check", new_callable=AsyncMock, return_value={"ok": True}), \
                patch.object(bot, "run_check_news", new_callable=AsyncMock) as check, \
                patch.object(bot, "fetch_news_html", new_callable=AsyncMock, return_value=None):
            await bot.cmd_controlla(self.update(1), context)
        check.assert_not_awaited()
        self.assertFalse(bot.NEWS_DB_PATH.exists())

    async def test_unreadable_first_news_page_with_next_page_is_rejected(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, text='<div class="card-wrapper"><h3>unreadable</h3></div><li id="btSucc"></li>',
            request=httpx.Request("GET", "https://example.invalid/news"),
        )
        with self.assertRaises(bot.IncompleteNewsSnapshot):
            await bot.fetch_all_news_pages(client, stop_at_known={"old"})
        client.get.assert_awaited_once()

    async def test_empty_first_news_page_without_pagination_remains_valid(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, text="<html><body>Nessuna news</body></html>",
            request=httpx.Request("GET", "https://example.invalid/news"),
        )
        self.assertEqual(await bot.fetch_all_news_pages(client, stop_at_known={"old"}), [])


if __name__ == "__main__":
    unittest.main()
