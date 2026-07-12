import json
import os
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_IDS", "1")

import bot


class PublicAlboArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "public"
        self.original_paths = (
            bot.PUBLIC_DATA_DIR,
            bot.ALBO_EVENTS_PATH,
            bot.ALBO_CURRENT_PATH,
            bot.ALBO_MANIFEST_PATH,
        )
        bot.PUBLIC_DATA_DIR = root
        bot.ALBO_EVENTS_PATH = root / "albo-events.ndjson"
        bot.ALBO_CURRENT_PATH = root / "albo-current.json"
        bot.ALBO_MANIFEST_PATH = root / "albo-manifest.json"

    def tearDown(self):
        (
            bot.PUBLIC_DATA_DIR,
            bot.ALBO_EVENTS_PATH,
            bot.ALBO_CURRENT_PATH,
            bot.ALBO_MANIFEST_PATH,
        ) = self.original_paths
        self.tmp.cleanup()

    @staticmethod
    def sample_item(title="Liquidazione fattura. CIG A01C429338 CUP G15E22000080006"):
        return {
            "title": title,
            "num_riga": "12",
            "date": "02-07-2026",
            "date_end": "17-07-2026",
            "expired": False,
            "tipo": "DETERMINAZIONI",
            "num_pub": "Pubblicazione n. 387",
            "sender": "Comune di Roccabascerana - Area tecnica",
            "act_number": "112",
            "register_number": "245",
            "allegati": [
                {"filename": "determina.pdf", "url": "https://temporary.invalid/session/file"}
            ],
            "_detail_text": title,
            "_detail_captured": True,
            "_detail_captured_at": "2026-07-12T09:00:00Z",
        }

    def read_current(self):
        return json.loads(bot.ALBO_CURRENT_PATH.read_text(encoding="utf-8"))

    def read_events(self):
        return [
            json.loads(line)
            for line in bot.ALBO_EVENTS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_first_snapshot_is_cumulative_and_privacy_safe(self):
        changed = bot.update_public_albo_archive(
            [self.sample_item()], "2026-07-12T09:00:00Z"
        )
        self.assertIn(str(bot.ALBO_CURRENT_PATH), changed)
        current = self.read_current()
        self.assertTrue(current["complete"])
        self.assertEqual(len(current["acts"]), 1)
        act = current["acts"][0]
        self.assertEqual(act["id"], "ALBO:e1396:2026:387")
        self.assertEqual(act["revision"], 1)
        self.assertEqual(act["cigs"], ["A01C429338"])
        self.assertEqual(act["cups"], ["G15E22000080006"])
        self.assertTrue(act["procurement"]["relevant"])
        self.assertEqual(act["attachments"][0]["privacyStatus"], "metadata_only")
        self.assertNotIn("url", act["attachments"][0])
        self.assertEqual(len(self.read_events()), 1)

    def test_identical_check_does_not_duplicate_event(self):
        item = self.sample_item()
        bot.update_public_albo_archive([item], "2026-07-12T09:00:00Z")
        changed = bot.update_public_albo_archive([item], "2026-07-12T10:00:00Z")
        self.assertEqual(changed, [])
        self.assertEqual(len(self.read_events()), 1)

    def test_detail_backfill_is_recorded_even_without_content_changes(self):
        item = self.sample_item("Avviso generico")
        item["allegati"] = []
        item.pop("_detail_text")
        item.pop("_detail_captured")
        item.pop("_detail_captured_at")
        bot.update_public_albo_archive([item], "2026-07-12T09:00:00Z")

        enriched = dict(item)
        enriched.update({
            "_detail_text": "Avviso generico",
            "_detail_captured": True,
            "_detail_captured_at": "2026-07-12T10:00:00Z",
        })
        changed = bot.update_public_albo_archive([enriched], "2026-07-12T10:00:00Z")
        self.assertIn(str(bot.ALBO_CURRENT_PATH), changed)
        self.assertEqual(self.read_current()["acts"][0]["detailCapturedAt"], "2026-07-12T10:00:00Z")
        self.assertEqual(len(self.read_events()), 1)

    def test_title_change_creates_revision_without_changing_public_id(self):
        bot.update_public_albo_archive([self.sample_item()], "2026-07-12T09:00:00Z")
        changed_item = self.sample_item("Liquidazione fattura corretta. CIG A01C429338")
        bot.update_public_albo_archive([changed_item], "2026-07-12T11:00:00Z")
        act = self.read_current()["acts"][0]
        self.assertEqual(act["id"], "ALBO:e1396:2026:387")
        self.assertEqual(act["revision"], 2)
        self.assertEqual(len(self.read_events()), 2)

    def test_missing_from_live_list_is_retained_and_marked_unpublished(self):
        bot.update_public_albo_archive([self.sample_item()], "2026-07-12T09:00:00Z")
        bot.update_public_albo_archive([], "2026-07-12T12:00:00Z")
        act = self.read_current()["acts"][0]
        self.assertFalse(act["currentlyPublished"])
        self.assertEqual(act["revision"], 2)
        self.assertEqual(self.read_events()[-1]["eventType"], "unpublished")

    def test_failure_preserves_last_valid_snapshot(self):
        bot.update_public_albo_archive([self.sample_item()], "2026-07-12T09:00:00Z")
        bot.record_public_archive_failure("fonte non raggiungibile", "2026-07-13T09:00:00Z")
        current = self.read_current()
        self.assertEqual(len(current["acts"]), 1)
        self.assertEqual(current["sync"]["status"], "stale")
        self.assertEqual(len(self.read_events()), 1)


if __name__ == "__main__":
    unittest.main()
