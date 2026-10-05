"""Offline service properties, including explicit counterexamples to the old model."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from runtime_policy import RuntimePolicy, MAX_RUNTIME_SECONDS, SETUP_BUDGET_SECONDS
from scheduling_simulator import Trigger, simulate, scheduled_triggers


START = datetime.fromisoformat('2026-10-01T00:00:00+02:00').astimezone(timezone.utc)


class SchedulingSystemTests(unittest.TestCase):
    def assert_policy_matches_real_time(self, result):
        policy = RuntimePolicy()
        for span in result.profiles:
            cursor = span.start
            while cursor < span.end:
                self.assertEqual(span.automatic_enabled, policy.allows(cursor), (span, cursor))
                self.assertEqual(span.interval_minutes, 15 if policy.allows(cursor) else None)
                cursor += timedelta(minutes=1)

    def assert_no_overlapping_writers(self, result):
        for previous, following in zip(result.runs, result.runs[1:]):
            self.assertLessEqual(previous.released_at, following.admitted_at)

    def test_real_incident_is_counterexample_to_legacy_and_passes_new_policy(self):
        nominal = datetime.fromisoformat('2026-10-01T03:37:00+02:00')
        trigger = Trigger('incident', nominal, delay_seconds=(6 * 60 + 4) * 60,
                          nominal_cron='37 3 * * *')
        end = START + timedelta(hours=24)
        old = simulate([trigger], START, end, strategy='legacy_by_event')
        new = simulate([trigger], START, end)
        self.assertEqual(old.runs[0].bot_started_at, datetime.fromisoformat('2026-10-01T09:41:00+02:00'))
        self.assertEqual(old.runs[0].bot_stopped_at - old.runs[0].bot_started_at, timedelta(minutes=55))
        with self.assertRaises(AssertionError):
            self.assert_policy_matches_real_time(old)
        self.assert_policy_matches_real_time(new)
        self.assertEqual(new.runs[0].bot_stopped_at - new.runs[0].bot_started_at, timedelta(hours=5))

    def test_inverse_incident_day_trigger_at_night_does_not_poll(self):
        trigger = Trigger('inverse', START - timedelta(hours=5, minutes=43),
                          delay_seconds=(5 * 60 + 56) * 60, nominal_cron='17 18 * * *')
        old = simulate([trigger], START, START + timedelta(hours=7), strategy='legacy_by_event')
        new = simulate([trigger], START, START + timedelta(hours=7))
        self.assertTrue(old.automatic)
        self.assertFalse(new.automatic)
        self.assertTrue(new.telegram)

    def test_24_and_48_hours_delay_matrix_preserves_service_properties(self):
        for hours in (24, 48):
            for delay in (0, 5, 30, 60, 180):
                with self.subTest(hours=hours, delay=delay):
                    end = START + timedelta(hours=hours)
                    triggers = scheduled_triggers(START - timedelta(days=1), end,
                                                  delay_seconds=delay * 60, setup_seconds=120,
                                                  cleanup_seconds=60)
                    result = simulate(triggers, START, end)
                    self.assert_policy_matches_real_time(result)
                    self.assert_no_overlapping_writers(result)
                    self.assertEqual(sum(x.seconds for x in result.telegram + result.gaps), hours * 3600)
                    self.assertTrue(result.replaced)
                    self.assertTrue(result.gaps)  # Setup/finalization are not Telegram uptime.
                    old_triggers = scheduled_triggers(START - timedelta(days=1), end, schedule='legacy',
                                                      delay_seconds=delay * 60, setup_seconds=120,
                                                      cleanup_seconds=60)
                    old = simulate(old_triggers, START, end, strategy='legacy_by_event')
                    self.assert_no_overlapping_writers(old)
                    with self.assertRaises(AssertionError):
                        self.assert_policy_matches_real_time(old)

    def test_different_delays_per_trigger_reorder_arrivals_without_wrong_profile(self):
        end = START + timedelta(hours=48)
        triggers = scheduled_triggers(START - timedelta(days=1), end, setup_seconds=60)
        delays = (180, 0, 60, 5, 30)
        triggers = [replace(t, delay_seconds=delays[i % len(delays)] * 60) for i, t in enumerate(triggers)]
        result = simulate(triggers, START, end)
        self.assert_policy_matches_real_time(result)
        self.assert_no_overlapping_writers(result)
        self.assertTrue(result.replaced)

    def test_legacy_48h_ideal_model_leaves_602_minutes_without_process(self):
        end = START + timedelta(hours=48)
        triggers = scheduled_triggers(START - timedelta(days=1), end, schedule='legacy')
        result = simulate(triggers, START, end, strategy='legacy_by_event')
        self.assertEqual(sum(span.seconds for span in result.gaps) / 60, 602)
        with self.assertRaises(AssertionError):
            self.assert_policy_matches_real_time(result)

    def test_latest_pending_replaces_previous_and_cleanup_keeps_lock(self):
        triggers = [Trigger('active', START, runtime_seconds=600, cleanup_seconds=120)]
        triggers += [Trigger(str(i), START + timedelta(minutes=i), runtime_seconds=60) for i in (1, 2, 3)]
        result = simulate(triggers, START, START + timedelta(hours=1))
        self.assertEqual(result.queued, ('1', '2', '3'))
        self.assertEqual(result.replaced, ('1', '2'))
        self.assertEqual([r.trigger.run_id for r in result.runs], ['active', '3'])
        self.assertEqual(result.runs[1].admitted_at, START + timedelta(minutes=12))
        self.assertEqual(result.runs[1].queue_seconds, 9 * 60)
        self.assert_no_overlapping_writers(result)

    def test_active_and_pending_can_preexist_the_observation_horizon(self):
        triggers = [Trigger('active', START - timedelta(minutes=30), runtime_seconds=3600),
                    Trigger('old-pending', START - timedelta(minutes=10)),
                    Trigger('latest', START + timedelta(minutes=5))]
        result = simulate(triggers, START, START + timedelta(hours=2))
        self.assertEqual(result.telegram[0].start, START)
        self.assertIn('old-pending', result.replaced)
        self.assertEqual(result.runs[1].trigger.run_id, 'latest')

    def test_crash_hands_over_to_pending_but_cannot_invent_a_successor(self):
        crashed = Trigger('crashed', START, crash_after_seconds=600, cleanup_seconds=60)
        alone = simulate([crashed], START, START + timedelta(hours=1))
        self.assertEqual(alone.runs[0].outcome, 'crashed')
        self.assertEqual(alone.gaps[-1].seconds, 50 * 60)
        successor = Trigger('successor', START + timedelta(minutes=5))
        handover = simulate([crashed, successor], START, START + timedelta(hours=1))
        self.assertEqual(handover.runs[1].admitted_at, START + timedelta(minutes=11))
        self.assert_no_overlapping_writers(handover)

    def test_manual_dispatch_has_same_current_policy_and_five_hour_lifetime(self):
        manual = Trigger('manual', START + timedelta(hours=1), event_name='workflow_dispatch')
        result = simulate([manual], START, START + timedelta(hours=8))
        self.assert_policy_matches_real_time(result)
        self.assertEqual(result.runs[0].bot_stopped_at - result.runs[0].bot_started_at, timedelta(hours=5))
        legacy = simulate([manual], START, START + timedelta(hours=8), strategy='legacy_by_event')
        self.assertEqual(legacy.runs[0].bot_stopped_at - legacy.runs[0].bot_started_at, timedelta(minutes=10))

    def test_day_and_night_boundaries_split_same_run_without_stopping_telegram(self):
        for hour, enabled in ((6, [False, True]), (19, [True, False])):
            with self.subTest(hour=hour):
                trigger = Trigger('crossing', START + timedelta(hours=hour), runtime_seconds=7200)
                result = simulate([trigger], START, START + timedelta(hours=26))
                self.assertEqual([p.automatic_enabled for p in result.profiles], enabled)
                self.assertEqual(len(result.telegram), 1)
                self.assertEqual(result.telegram[0].seconds, 7200)

    def test_slow_setup_hits_old_job_timeout_but_new_setup_is_bounded(self):
        trigger = Trigger('slow', START, setup_seconds=30 * 60, cleanup_seconds=60,
                          nominal_cron='57 6 * * *')
        end = START + timedelta(hours=8)
        old = simulate([trigger], START, end, strategy='legacy_by_event')
        new = simulate([trigger], START, end)
        self.assertEqual(old.runs[0].outcome, 'job_timeout')
        self.assertEqual(old.runs[0].released_at - old.runs[0].job_started_at, timedelta(minutes=355))
        self.assertEqual(new.runs[0].outcome, 'setup_timeout')
        self.assertIsNone(new.runs[0].bot_started_at)
        self.assertEqual(new.runs[0].released_at - START, timedelta(seconds=SETUP_BUDGET_SECONDS + 60))

    def test_valid_slow_setup_preserves_fixed_runtime_and_cleanup_budget(self):
        trigger = Trigger('bounded', START, setup_seconds=18 * 60, shutdown_seconds=120, cleanup_seconds=540)
        result = simulate([trigger], START, START + timedelta(hours=8))
        run = result.runs[0]
        self.assertEqual(run.outcome, 'completed')
        self.assertEqual((run.bot_stopped_at - run.bot_started_at).total_seconds(), MAX_RUNTIME_SECONDS)
        self.assertLess((run.released_at - run.job_started_at).total_seconds(), 355 * 60)

    def test_impossible_custom_budget_is_refused_without_shortening_runtime(self):
        result = simulate([Trigger('invalid-budget', START)], START, START + timedelta(hours=8),
                          job_timeout_seconds=18000)
        self.assertEqual(result.runs[0].outcome, 'budget_refused')
        self.assertFalse(result.telegram)

    def test_runner_queue_is_not_execution_time_but_still_holds_concurrency(self):
        trigger = Trigger('runner-delay', START, runner_wait_seconds=7200)
        result = simulate([trigger, Trigger('next', START + timedelta(hours=1))],
                          START, START + timedelta(hours=10))
        first = result.runs[0]
        self.assertEqual(first.bot_started_at, START + timedelta(hours=2))
        self.assertEqual(first.bot_stopped_at, START + timedelta(hours=7))
        self.assert_no_overlapping_writers(result)

    def test_dst_48h_scenarios_preserve_real_time_policy(self):
        for begin in ('2026-03-28T00:00:00+00:00', '2026-10-24T00:00:00+00:00'):
            start = datetime.fromisoformat(begin)
            end = start + timedelta(hours=48)
            for ambiguous in ('first', 'second', 'both'):
                with self.subTest(begin=begin, ambiguous=ambiguous):
                    triggers = scheduled_triggers(start - timedelta(days=1), end, ambiguous=ambiguous)
                    result = simulate(triggers, start, end)
                    self.assert_policy_matches_real_time(result)
                    self.assert_no_overlapping_writers(result)
                    self.assertEqual(sum(x.seconds for x in result.telegram + result.gaps), 48 * 3600)

    def test_timezone_generator_makes_ambiguous_occurrence_an_explicit_input(self):
        start = datetime.fromisoformat('2026-10-25T00:00:00+02:00')
        end = datetime.fromisoformat('2026-10-26T00:00:00+01:00')
        self.assertEqual(len(scheduled_triggers(start, end, ambiguous='both')), 25)
        self.assertEqual(len(scheduled_triggers(start, end, ambiguous='first')), 24)
        self.assertEqual(len(scheduled_triggers(start, end, ambiguous='second')), 24)

    def test_unsupported_concurrency_and_duplicate_ids_are_rejected(self):
        trigger = Trigger('same', START)
        for kwargs in ({'cancel_in_progress': True}, {'concurrency': 'fifo'}):
            with self.assertRaises(ValueError):
                simulate([trigger], START, START + timedelta(hours=1), **kwargs)
        with self.assertRaises(ValueError):
            simulate([trigger, trigger], START, START + timedelta(hours=1))


if __name__ == '__main__':
    unittest.main()
