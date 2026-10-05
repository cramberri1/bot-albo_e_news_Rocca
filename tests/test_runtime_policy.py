from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError
from pathlib import Path
import yaml

from runtime_policy import (
    RuntimePolicy, MAX_RUNTIME_SECONDS, JOB_TIMEOUT_SECONDS,
    SETUP_BUDGET_SECONDS, SHUTDOWN_GRACE_SECONDS, FINALIZATION_BUDGET_SECONDS,
)


def instant(value):
    return datetime.fromisoformat(value)


class RuntimePolicyTests(unittest.TestCase):
    def test_selected_window_is_half_open_and_independent_of_legacy_environment(self):
        policy = RuntimePolicy.from_environment({'INTERVAL_MINUTES': '30', 'SCHEDULE': '37 3 * * *'})
        self.assertEqual((policy.start_label, policy.end_label, policy.interval_minutes), ('07:00', '20:00', 15))
        for time, expected in [('06:59:59', False), ('07:00:00', True),
                               ('19:59:59', True), ('20:00:00', False)]:
            with self.subTest(time=time):
                self.assertEqual(policy.allows(instant(f'2026-10-01T{time}+02:00')), expected)

    def test_configuration_is_explicit_and_can_cross_midnight(self):
        policy = RuntimePolicy.from_environment({
            'AUTO_POLL_START': '22:00', 'AUTO_POLL_END': '06:00', 'AUTO_POLL_INTERVAL_MINUTES': '20'})
        self.assertEqual(policy.interval_minutes, 20)
        self.assertTrue(policy.allows(instant('2026-10-01T23:00:00+02:00')))
        self.assertTrue(policy.allows(instant('2026-10-02T05:59:00+02:00')))
        self.assertFalse(policy.allows(instant('2026-10-02T06:00:00+02:00')))

    def test_end_override_keeps_evening_polling_available_when_explicitly_selected(self):
        policy = RuntimePolicy.from_environment({'AUTO_POLL_END': '23:00'})
        self.assertTrue(policy.allows(instant('2026-10-01T20:00:00+02:00')))
        self.assertTrue(policy.allows(instant('2026-10-01T22:59:59+02:00')))
        self.assertFalse(policy.allows(instant('2026-10-01T23:00:00+02:00')))

    def test_invalid_configuration_fails_instead_of_falling_back(self):
        for values in [
            {'AUTO_POLL_START': '7:00'}, {'AUTO_POLL_END': '24:00'},
            {'AUTO_POLL_START': '20:00'}, {'AUTO_POLL_INTERVAL_MINUTES': '0'},
            {'AUTO_POLL_INTERVAL_MINUTES': '-1'}, {'AUTO_POLL_INTERVAL_MINUTES': '1.5'},
            {'AUTO_POLL_INTERVAL_MINUTES': '1441'}, {'AUTO_POLL_INTERVAL_MINUTES': ''},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                RuntimePolicy.from_environment(values)
        with self.assertRaises(ValueError):
            RuntimePolicy(interval_minutes=True)

    def test_missing_timezone_database_is_not_silently_utc(self):
        with patch('runtime_policy.ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')):
            with self.assertRaises(ZoneInfoNotFoundError):
                RuntimePolicy()

    def test_naive_datetime_is_rejected_by_both_operations(self):
        policy = RuntimePolicy()
        for operation in (policy.allows, policy.next_transition):
            with self.assertRaises(ValueError):
                operation(datetime(2026, 10, 1, 9, 41))

    def test_boundary_is_future_utc_and_microseconds_do_not_shift_it(self):
        policy = RuntimePolicy()
        now = instant('2026-10-01T06:59:59.900000+02:00')
        self.assertEqual(policy.next_transition(now), instant('2026-10-01T05:00:00+00:00'))
        self.assertEqual(policy.next_transition(instant('2026-10-01T07:00:00+02:00')),
                         instant('2026-10-01T18:00:00+00:00'))
        self.assertEqual(policy.next_transition(now).tzinfo, timezone.utc)

    def test_spring_and_autumn_nights_have_real_ten_and_twelve_hour_durations(self):
        policy = RuntimePolicy()
        for evening, hours in [('2026-03-28T20:00:00+01:00', 10), ('2026-10-24T20:00:00+02:00', 12)]:
            now = instant(evening)
            with self.subTest(evening=evening):
                self.assertEqual(policy.next_transition(now) - now, timedelta(hours=hours))
                self.assertTrue(policy.allows(policy.next_transition(now)))

    def test_both_occurrences_of_autumn_two_oclock_are_night(self):
        policy = RuntimePolicy()
        for value in ('2026-10-25T02:30:00+02:00', '2026-10-25T02:30:00+01:00'):
            self.assertFalse(policy.allows(instant(value)))
            self.assertEqual(policy.next_transition(instant(value)), instant('2026-10-25T06:00:00+00:00'))

    def test_transition_inside_repeated_hour_is_detected(self):
        policy = RuntimePolicy(start_label='01:30', end_label='02:30')
        self.assertEqual(policy.next_transition(instant('2026-10-25T00:45:00+00:00')),
                         instant('2026-10-25T01:00:00+00:00'))

    def test_window_skipped_by_spring_transition_waits_until_next_real_day(self):
        policy = RuntimePolicy(start_label='02:10', end_label='02:20')
        self.assertEqual(policy.next_transition(instant('2026-03-28T01:21:00+00:00')),
                         instant('2026-03-30T00:10:00+00:00'))

    def test_fixed_runtime_leaves_explicit_setup_shutdown_and_recovery_budget(self):
        self.assertEqual(MAX_RUNTIME_SECONDS, 5 * 3600)
        self.assertLess(SETUP_BUDGET_SECONDS + MAX_RUNTIME_SECONDS + SHUTDOWN_GRACE_SECONDS
                        + FINALIZATION_BUDGET_SECONDS, JOB_TIMEOUT_SECONDS)

    def test_setup_budget_matches_deployed_workflow_step_limits(self):
        workflow = yaml.load(Path('.github/workflows/albo_check.yml').read_text(encoding='utf-8-sig'),
                             Loader=yaml.BaseLoader)
        seconds = 0
        for step in workflow['jobs']['check']['steps']:
            if step['name'] == 'Esegui bot':
                break
            seconds += int(step['timeout-minutes']) * 60
        self.assertEqual(SETUP_BUDGET_SECONDS, seconds)


if __name__ == '__main__':
    unittest.main()
