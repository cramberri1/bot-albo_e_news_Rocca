from pathlib import Path
import unittest
import yaml
from runtime_policy import JOB_TIMEOUT_SECONDS, SETUP_BUDGET_SECONDS, FINALIZATION_BUDGET_SECONDS


class WorkflowTests(unittest.TestCase):
    def test_worker_wake_timezone_and_bounded_lifetime(self):
        workflow = yaml.load(Path('.github/workflows/albo_check.yml').read_text(encoding='utf-8-sig'), Loader=yaml.BaseLoader)
        schedules = workflow['on']['schedule']
        self.assertEqual({s['cron'] for s in schedules}, {'17 * * * *'})
        self.assertTrue(all(s['timezone'] == 'Europe/Rome' for s in schedules))
        self.assertEqual(workflow['concurrency'], {'group': 'albo-rocca-bot', 'cancel-in-progress': 'false'})
        self.assertIn('workflow_dispatch', workflow['on'])
        dispatch_inputs = workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(dispatch_inputs['run_seconds']['default'], '18000')
        self.assertEqual(dispatch_inputs['run_seconds']['type'], 'string')
        self.assertIn('recovery_from_run_id', dispatch_inputs)
        self.assertLess(int(workflow['jobs']['check']['timeout-minutes']), 360)
        steps = workflow['jobs']['check']['steps']
        source = Path('.github/workflows/albo_check.yml').read_text(encoding='utf-8-sig')
        self.assertNotIn('github.event.schedule', source)
        self.assertNotIn('github.event_name', source)
        self.assertEqual(workflow['env']['BOT_SECONDS'], '18000')
        self.assertEqual(workflow['env']['AUTO_POLL_START'], '07:00')
        self.assertEqual(workflow['env']['AUTO_POLL_END'], '20:00')
        self.assertEqual(workflow['env']['AUTO_POLL_INTERVAL_MINUTES'], '15')
        # Every potentially blocking step has a budget, including setup and
        # recovery, rather than an assumption that setup will take eight min.
        self.assertTrue(all(int(s['timeout-minutes']) > 0 for s in steps))
        self.assertLess(sum(int(s['timeout-minutes']) for s in steps),
                        int(workflow['jobs']['check']['timeout-minutes']))
        execution = next(s for s in steps if s['name'] == 'Esegui bot')
        execution_index = steps.index(execution)
        self.assertEqual(sum(int(s['timeout-minutes']) for s in steps[:execution_index]) * 60,
                         SETUP_BUDGET_SECONDS)
        self.assertEqual(sum(int(s['timeout-minutes']) for s in steps[execution_index + 1:]) * 60,
                         FINALIZATION_BUDGET_SECONDS)
        self.assertEqual(int(workflow['jobs']['check']['timeout-minutes']) * 60, JOB_TIMEOUT_SECONDS)
        self.assertIn('--signal=INT --kill-after=120s', execution['run'])
        self.assertIn('timeout --preserve-status', execution['run'])
        self.assertNotIn('"$code" -eq 124', execution['run'])
        self.assertGreater(int(execution['timeout-minutes']) * 60, 18000 + 120)
        self.assertIn('exit "$code"', execution['run'])
        self.assertEqual(execution['env']['BOT_SECONDS_INPUT'], '${{ inputs.run_seconds }}')
        self.assertIn('BOT_SECONDS="$(python workflow_recovery.py duration)"', execution['run'])
        self.assertLess(execution['run'].index('workflow_recovery.py duration'),
                        execution['run'].index('\nset +e'))
        self.assertNotIn('${{ inputs.run_seconds }}', execution['run'])
        final = next(s for s in steps if s['name'] == 'Salva eventuale stato residuo')
        self.assertEqual(final['if'], 'always()')
        self.assertIn('HEAD:main', final['run'])
        self.assertIn('data/telegram_updates.json', final['run'])
        self.assertIn('data/albo_safety.json', final['run'])
        self.assertIn('data/albo_baseline.json', final['run'])
        self.assertIn('data/news_baseline.json', final['run'])
        recovery = next(s for s in steps if s['name'] == 'Conserva stato per recupero dopo errore')
        self.assertEqual(recovery['if'], 'failure()')
        self.assertEqual(recovery['with']['retention-days'], '3')
        self.assertIn('data/albo_baseline.json', recovery['with']['path'])
        self.assertIn('data/news_baseline.json', recovery['with']['path'])

    def test_recovery_is_separate_trusted_and_cannot_recurse_dispatches(self):
        workflow = yaml.load(Path('.github/workflows/albo_recovery.yml').read_text(encoding='utf-8-sig'),
                             Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on'], {'workflow_run': {
            'workflows': ['Albo Pretorio Check'], 'types': ['completed'], 'branches': ['main']}})
        self.assertEqual(workflow['permissions'], {'contents': 'read', 'actions': 'write'})
        self.assertEqual(workflow['concurrency'], {
            'group': 'albo-rocca-recovery', 'cancel-in-progress': 'false'})
        job = workflow['jobs']['recover']
        self.assertIn("event == 'schedule'", job['if'])
        self.assertIn('run_attempt == 1', job['if'])
        self.assertIn("conclusion == 'failure'", job['if'])
        self.assertIn("conclusion == 'timed_out'", job['if'])
        self.assertNotIn('cancelled', job['if'])
        checkout = job['steps'][0]
        self.assertEqual(checkout['with']['ref'], 'main')
        self.assertEqual(checkout['with']['persist-credentials'], 'false')
        self.assertRegex(checkout['uses'], r'^actions/checkout@[0-9a-f]{40}$')
        self.assertLessEqual(sum(int(step['timeout-minutes']) for step in job['steps']),
                             int(job['timeout-minutes']))
        source = Path('.github/workflows/albo_recovery.yml').read_text(encoding='utf-8-sig')
        for forbidden in ('BOT_TOKEN', 'CHAT_IDS', 'STATE_ENCRYPTION_KEY', 'download-artifact',
                          'github.event.workflow_run.head_sha', 'pip install', 'cache:'):
            self.assertNotIn(forbidden, source)

    def test_ci_has_history_for_forensic_tests(self):
        workflow = yaml.load(Path('.github/workflows/tests.yml').read_text(encoding='utf-8-sig'), Loader=yaml.BaseLoader)
        checkout = workflow['jobs']['tests']['steps'][0]
        self.assertEqual(checkout['with']['fetch-depth'], '0')
        self.assertIn('requirements-dev.txt', workflow['on']['push']['paths'])
