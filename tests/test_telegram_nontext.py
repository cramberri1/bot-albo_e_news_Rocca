"""A non-text Telegram update must not stop polling after its durable claim."""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call, patch

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)
import bot


class TelegramNonTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_text_update_is_processed_and_polling_continues(self):
        for text, callback in [(None, False), ('', False), ('   ', False), (None, True)]:
            with self.subTest(text=text, callback=callback), tempfile.TemporaryDirectory() as temp:
                stop = asyncio.Event()
                app = AsyncMock()
                updates = [
                    SimpleNamespace(
                        update_id=number,
                        effective_message=SimpleNamespace(
                            text=value, date=datetime.now(timezone.utc)
                        ),
                        callback_query=is_callback,
                    )
                    for number, value, is_callback in [
                        (123, text, callback), (124, '/help@ExampleBot', False)
                    ]
                ]
                app.bot.get_updates.return_value = updates

                async def process(update):
                    if update.update_id == 124:
                        stop.set()

                app.process_update.side_effect = process
                with patch.object(bot, 'TELEGRAM_UPDATES_PATH', Path(temp) / 'updates.json'), \
                     patch.object(bot, 'git_commit_and_push', return_value=True):
                    await bot.telegram_polling(app, stop)
                    state = bot.load_telegram_updates()

                self.assertEqual(app.process_update.await_args_list, [call(u) for u in updates])
                self.assertEqual(state['recent_ids'], [123, 124])
                self.assertEqual(state['last_processed_update_id'], 124)
                self.assertNotIn('in_progress', state)
                app.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)


if __name__ == '__main__':
    unittest.main()
