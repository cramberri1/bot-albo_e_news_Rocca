"""Characterize residual failure semantics; these tests do not promise exactly-once."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_IDS", "1")
os.environ.pop("GITHUB_ACTIONS", None)

import bot
from telegram.error import TimedOut


class DeliveryFailureSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_claim_then_crash_before_handler_loses_effect_without_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "telegram_updates.json"
            confirmed_remote = None

            def persist(*_args, **_kwargs):
                nonlocal confirmed_remote
                confirmed_remote = path.read_bytes()
                return True

            def update(number):
                return SimpleNamespace(update_id=number, effective_message=None, callback_query=True)

            original_claim = bot.claim_telegram_update

            def crash_after_claim(number):
                self.assertTrue(original_claim(number))
                raise asyncio.CancelledError()

            first_app = AsyncMock()
            first_app.bot.get_updates.return_value = [update(123)]
            with patch.object(bot, "TELEGRAM_UPDATES_PATH", path), \
                    patch.object(bot, "git_commit_and_push", side_effect=persist):
                with patch.object(bot, "claim_telegram_update", side_effect=crash_after_claim):
                    with self.assertRaises(asyncio.CancelledError):
                        await bot.telegram_polling(first_app, asyncio.Event())
                first_app.process_update.assert_not_awaited()
                claimed = json.loads(confirmed_remote)
                self.assertEqual(claimed["in_progress"], 123)
                self.assertIn(123, claimed["recent_ids"])
                self.assertNotIn("last_processed_update_id", claimed)

                # A new runner sees only the confirmed remote checkpoint.
                path.write_bytes(confirmed_remote)
                restarted_app = AsyncMock()
                queued = [update(123), update(124)]
                restarted_app.bot.get_updates.return_value = queued
                stop = asyncio.Event()

                async def process_next(received):
                    self.assertEqual(received.update_id, 124)
                    stop.set()

                restarted_app.process_update.side_effect = process_next
                await bot.telegram_polling(restarted_app, stop)
                restarted_app.process_update.assert_awaited_once_with(queued[1])
                self.assertEqual(bot.load_telegram_updates()["last_processed_update_id"], 124)
                # Update 123 never invoked a handler, and still was not retried.
                first_app.process_update.assert_not_awaited()

    async def test_accepted_document_then_response_timeout_can_duplicate_on_retry(self):
        telegram = AsyncMock()
        accepted_documents = []
        payload = b"%PDF-1.4 fixture document"

        async def accept_then_timeout(**request):
            accepted_documents.append(request["document"])
            if len(accepted_documents) == 1:
                # Telegram accepted the bytes, but its response never arrived.
                raise TimedOut("simulated response timeout")

        telegram.send_document.side_effect = accept_then_timeout
        item = {
            "title": "Fixture document", "num_pub": "1",
            "_attachment_fetch_ok": True, "_attachment_expected_count": 1,
            "allegati": [{"filename": "fixture.pdf", "content": payload}],
        }
        with patch.object(bot, "_CHAT_DELIVERY_LOCKS", {}), \
                patch.object(bot, "MAX_RETRIES", 2), \
                patch.object(bot.asyncio, "sleep", new_callable=AsyncMock), \
                patch.object(bot.httpx, "AsyncClient", side_effect=AssertionError("Unexpected network")):
            complete = await bot.send_item_to_chat(telegram, 1, item)
        self.assertTrue(complete)
        telegram.send_message.assert_awaited_once()
        self.assertEqual(telegram.send_document.await_count, 2)
        self.assertEqual(accepted_documents, [payload, payload])

    async def test_synchronous_git_push_blocks_a_ready_event_loop_callback(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            state_dir = repository / "data"
            state_dir.mkdir()
            state = state_dir / "seen.json"
            state.write_text("{}", encoding="utf-8")
            events = []
            asyncio.get_running_loop().call_soon(events.append, "ready_callback")

            def git_process(command, **_kwargs):
                if "rev-parse" in command:
                    return subprocess.CompletedProcess(command, 0, str(repository) + "\n", "")
                if "push" in command:
                    events.append("push_started")
                    # Inject a 20 ms synchronous wait, never an actual Git/network call.
                    threading.Event().wait(0.02)
                    events.append("push_finished")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "STATE_GIT_BRANCH": "main"}), \
                    patch.object(bot, "DATA_DIR", state_dir), \
                    patch("bot.GitCommandRunner.run", side_effect=git_process) as run:
                self.assertTrue(bot.git_commit_and_push([str(state)]))
            self.assertTrue(any("push" in call.args[0] for call in run.call_args_list))
            self.assertEqual(events, ["push_started", "push_finished"])
            await asyncio.sleep(0)
            self.assertEqual(events, ["push_started", "push_finished", "ready_callback"])


if __name__ == "__main__":
    unittest.main()
