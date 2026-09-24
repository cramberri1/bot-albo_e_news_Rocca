from pathlib import Path
import unittest
import yaml


class WorkflowTests(unittest.TestCase):
    def test_five_profiles_timezone_and_limits(self):
        workflow = yaml.load(Path('.github/workflows/albo_check.yml').read_text(encoding='utf-8-sig'), Loader=yaml.BaseLoader)
        schedules = workflow['on']['schedule']
        self.assertEqual({s['cron'] for s in schedules}, {'57 6 * * *', '37 12 * * *', '17 18 * * *', '7 0 * * *', '37 3 * * *'})
        self.assertTrue(all(s['timezone'] == 'Europe/Rome' for s in schedules))
        self.assertEqual(workflow['concurrency'], {'group': 'albo-rocca-bot', 'cancel-in-progress': 'false'})
        self.assertIn('workflow_dispatch', workflow['on'])
        self.assertLess(int(workflow['jobs']['check']['timeout-minutes']), 360)
        steps = workflow['jobs']['check']['steps']
        profile = next(s['run'] for s in steps if s['name'] == 'Seleziona profilo')
        for token in ['BOT_SECONDS=20580', 'BOT_SECONDS=3300', 'BOT_SECONDS=600', 'INTERVAL_MINUTES=15', 'INTERVAL_MINUTES=30']:
            self.assertIn(token, profile)
        self.assertLess(20580 / 60 + 2 + 8, int(workflow['jobs']['check']['timeout-minutes']))
        final = next(s for s in steps if s['name'] == 'Salva eventuale stato residuo')
        self.assertEqual(final['if'], 'always()')
        self.assertIn('HEAD:main', final['run'])
        self.assertIn('data/telegram_updates.json', final['run'])
        self.assertIn('data/albo_safety.json', final['run'])

    def test_ci_has_history_for_forensic_tests(self):
        workflow = yaml.load(Path('.github/workflows/tests.yml').read_text(encoding='utf-8-sig'), Loader=yaml.BaseLoader)
        checkout = workflow['jobs']['tests']['steps'][0]
        self.assertEqual(checkout['with']['fetch-depth'], '0')
