import json
from pathlib import Path
import unittest
from scripts.repair_incident import at, plan, BEFORE, INCIDENT, REVISION


class IncidentRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Test CI fetches complete history; this proves the actual 2026-09-24 incident.
        cls.before, cls.incident, cls.revision = at(BEFORE), at(INCIDENT), at(REVISION)
        cls.evidence = json.loads(Path('docs/incident_identity_evidence.json').read_text(encoding='utf-8'))

    def test_exact_incident_repair_and_idempotence(self):
        repaired, report = plan(self.before, self.incident, self.revision, self.revision, self.evidence)
        self.assertEqual(len(report['duplicates']), 102)
        self.assertEqual(len(report['false_revisions']), 19)
        self.assertEqual(len(repaired), 363)
        self.assertEqual(report['skipped'], [])
        again, second = plan(self.before, self.incident, self.revision, repaired, self.evidence)
        self.assertEqual(again, repaired)
        self.assertEqual(second['duplicates'], [])
        self.assertEqual(second['false_revisions'], [])

    def test_subsequent_genuine_revision_is_preserved(self):
        import copy
        current = copy.deepcopy(self.revision)
        key = next(k for k in current if current[k].get('revision') == 2)
        current[key]['revision'] = 3
        current[key]['revision_snapshot']['attachment_sha256'] = ['real-new-content']
        repaired, _ = plan(self.before, self.incident, self.revision, current, self.evidence)
        self.assertEqual(repaired[key]['revision'], 2)
        self.assertEqual(repaired[key]['revision_snapshot']['attachment_sha256'], ['real-new-content'])

    def test_unproven_no_attachment_records_are_not_merged(self):
        _, report = plan(self.before, self.incident, self.revision, self.revision, {})
        self.assertEqual(len(report['duplicates']), 97)
        self.assertEqual(len(report['skipped']), 5)
