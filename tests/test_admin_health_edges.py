"""Runtime monitoring must not invent success or reuse stale cycle counters."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from admin_status import CheckHealth


class CheckHealthEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_details_are_visible_even_without_delivery_failures(self):
        monitor = CheckHealth()
        await monitor.run("Albo", AsyncMock(return_value={
            "ok": False, "failed": 0, "detail_failures": 2,
        }), None)
        self.assertIn("Albo: incompleto o bloccato", monitor.lines())
        self.assertIn("Consegne fallite nel ciclo: 0", monitor.lines())
        self.assertIn("Dettagli non verificati nel ciclo: 2", monitor.lines())
        self.assertIsNone(monitor.checks["Albo"]["last_success"])

    async def test_malformed_results_are_preserved_but_not_reported_as_success(self):
        for result in (None, [], "secret-value", {"ok": "yes"}, {"ok": 1}):
            with self.subTest(result=result):
                monitor = CheckHealth()
                self.assertIs(await monitor.run("Albo", AsyncMock(return_value=result), None), result)
                self.assertIn("Albo: incompleto o bloccato", monitor.lines())
                self.assertIsNone(monitor.checks["Albo"]["last_success"])
                self.assertIn("Consegne fallite nel ciclo: non disponibile", monitor.lines())
                self.assertNotIn("secret-value", "\n".join(monitor.lines()))

    async def test_invalid_counts_do_not_become_zero_or_disclose_strings(self):
        for invalid in (None, True, -1, 2.5, "token-example", 2**64):
            with self.subTest(invalid=invalid):
                monitor = CheckHealth()
                result = {"ok": True, "new": invalid, "failed": invalid}
                await monitor.run("News", AsyncMock(return_value=result), None)
                self.assertIn("News: completato", monitor.lines())
                self.assertIn("Nuovi nel ciclo: non disponibile", monitor.lines())
                self.assertIn("Consegne fallite nel ciclo: non disponibile", monitor.lines())
                self.assertNotIn("token-example", "\n".join(monitor.lines()))
        await monitor.run("News", AsyncMock(return_value={"ok": True, "failed": 0}), None)
        self.assertIn("Consegne fallite nel ciclo: 0", monitor.lines())

    async def test_error_clears_previous_counts_but_preserves_previous_success(self):
        for error in (RuntimeError("private-error-detail"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                monitor = CheckHealth()
                await monitor.run("Albo", AsyncMock(return_value={"ok": True, "new": 7, "failed": 4}), None)
                success = monitor.checks["Albo"]["last_success"]
                with self.assertRaises(type(error)) as caught:
                    await monitor.run("Albo", AsyncMock(side_effect=error), None)
                self.assertIs(caught.exception, error)
                self.assertEqual(monitor.checks["Albo"]["last_success"], success)
                self.assertIn("Nuovi nel ciclo: non disponibile", monitor.lines())
                self.assertIn("Consegne fallite nel ciclo: non disponibile", monitor.lines())
                self.assertNotIn("private-error-detail", repr(monitor.checks))
                self.assertIsNotNone(monitor.checks["Albo"]["duration_seconds"])

    async def test_running_attempt_clears_counts_and_does_not_claim_completion(self):
        monitor = CheckHealth()
        await monitor.run("News", AsyncMock(return_value={"ok": True, "new": 8}), None)
        entered, release = asyncio.Event(), asyncio.Event()

        async def check(_):
            entered.set()
            await release.wait()
            return {"ok": True}

        task = asyncio.create_task(monitor.run("News", check, None))
        try:
            await entered.wait()
            self.assertIn("News: in corso", monitor.lines())
            self.assertIsNone(monitor.checks["News"]["new"])
            self.assertIsNone(monitor.checks["News"]["duration_seconds"])
        finally:
            release.set()
            await task

    async def test_duration_and_uptime_use_monotonic_time(self):
        with patch("admin_status.time.monotonic", side_effect=[100.0, 105.0, 109.5, 112.0]):
            monitor = CheckHealth()
            await monitor.run("Albo", AsyncMock(return_value={"ok": True}), None)
            self.assertEqual(monitor.checks["Albo"]["duration_seconds"], 4.5)
            self.assertEqual(monitor.uptime_seconds, 12.0)
        self.assertIsNotNone(monitor.started_at.tzinfo)
        self.assertIsNotNone(monitor.checks["Albo"]["last_success"].tzinfo)


if __name__ == "__main__":
    unittest.main()
