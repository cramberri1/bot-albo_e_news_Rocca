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
        self.assertGreater(int(execution['timeout-minutes']) * 60, 18000 + 120)
        self.assertIn('exit "$code"', execution['run'])
        final = next(s for s in steps if s['name'] == 'Salva eventuale stato residuo')
        self.assertEqual(final['if'], 'always()')
        self.assertIn('HEAD:main', final['run'])
        self.assertIn('data/telegram_updates.json', final['run'])
        self.assertIn('data/albo_safety.json', final['run'])
        recovery = next(s for s in steps if s['name'] == 'Conserva stato per recupero dopo errore')
        self.assertEqual(recovery['if'], 'failure()')
        self.assertEqual(recovery['with']['retention-days'], '3')

    def test_ci_has_history_for_forensic_tests(self):
        workflow = yaml.load(Path('.github/workflows/tests.yml').read_text(encoding='utf-8-sig'), Loader=yaml.BaseLoader)
        checkout = workflow['jobs']['tests']['steps'][0]
        self.assertEqual(checkout['with']['fetch-depth'], '0')
        self.assertIn('requirements-dev.txt', workflow['on']['push']['paths'])
