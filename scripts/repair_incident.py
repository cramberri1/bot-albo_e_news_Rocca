"""Auditable, idempotent repair limited to the two proven incident commits.

Run from the repository root. Default is dry-run; --apply changes only seen_items.
No private state is read or decrypted.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from albo_identity import legacy_item_id, normalize_snapshot

BEFORE = 'b7d5e76a40133f4daa8cf1d9d137bfe0e3741297'
INCIDENT = '4718d27f0d3a5af433fbcbdc9963444a0345ea06'
REVISION = '25d503b7deea0893a0167b97c89d87b631f3f0cd'


def at(ref):
    return json.loads(subprocess.check_output(['git', 'show', ref + ':data/seen_items.json']))


def fingerprint(snapshot):
    raw = json.dumps(normalize_snapshot(snapshot), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def plan(before, incident, revision, current, evidence):
    repaired = copy.deepcopy(current)
    report = dict(duplicates=[], false_revisions=[], skipped=[])
    for new_id in sorted(set(incident) - set(before)):
        snapshot = normalize_snapshot(incident[new_id].get('revision_snapshot'))
        matches = [old_id for old_id, record in before.items()
                   if normalize_snapshot(record.get('revision_snapshot')) == snapshot]
        if len(matches) != 1:
            report['skipped'].append([new_id, 'ambiguous historical match'])
            continue
        old_id = matches[0]
        basis = 'date/sender/numbers/attachment count/names/SHA256 identical after placeholder normalization'
        if not snapshot['attachment_sha256']:
            proof = evidence.get(new_id, {})
            old = dict(title=proof.get('title'), num_pub=proof.get('num_pub'), tipo=proof.get('old_tipo'))
            new = dict(old, tipo=proof.get('new_tipo'))
            if proof.get('old_id') != old_id or legacy_item_id(old) != old_id or legacy_item_id(new) != new_id:
                report['skipped'].append([new_id, 'no attachment hashes and no exact ID reconstruction'])
                continue
            basis = 'exact old AND new SHA256 ID reconstruction from live title/publication/type + identical historical snapshot'
        if new_id not in repaired:
            if new_id in repaired.get(old_id, {}).get('legacy_ids', []):
                continue  # Already applied, do not re-count or mutate.
            report['skipped'].append([new_id, 'record missing without retained alias'])
            continue
        if old_id not in repaired:
            report['skipped'].append([new_id, 'canonical record missing'])
            continue
        old, new = repaired[old_id], repaired[new_id]
        # Never guess how to combine diverged genuine histories or pending delivery.
        if normalize_snapshot(old.get('revision_snapshot')) != normalize_snapshot(new.get('revision_snapshot')) or old.get('delivery_pending') or new.get('delivery_pending'):
            report['skipped'].append([new_id, 'current content diverged or delivery pending'])
            continue
        for record in (old, new):
            if normalize_snapshot(record.get('revision_snapshot')) != snapshot:
                break
        else:
            merged = {**old, **new}
            merged['revision'] = max(int(old.get('revision') or 1), int(new.get('revision') or 1))
            merged['notified'] = bool(old.get('notified', True) or new.get('notified', True))
            merged['legacy_ids'] = sorted(set(old.get('legacy_ids', [])) | set(new.get('legacy_ids', [])) | {new_id})
            merged['revision_snapshot'] = normalize_snapshot(merged['revision_snapshot'])
            merged['revision_fingerprint'] = fingerprint(merged['revision_snapshot'])
            merged['incident_reconciled_from'] = new_id
            repaired[old_id] = merged
            del repaired[new_id]
            report['duplicates'].append(dict(duplicate=new_id, canonical=old_id, basis=basis))
            continue
        report['skipped'].append([new_id, 'subsequent real revision needs manual audit'])

    for key, record in revision.items():
        previous = incident.get(key, {})
        if record.get('revision') == previous.get('revision'):
            continue
        old_snapshot, new_snapshot = previous.get('revision_snapshot'), record.get('revision_snapshot')
        if not old_snapshot or normalize_snapshot(old_snapshot) != normalize_snapshot(new_snapshot):
            continue
        target = repaired.get(key)
        if not target or target.get('incident_false_revision_repaired'):
            continue
        # Subtract just this proven false increment; preserve any later increments.
        delta = int(record['revision']) - int(previous.get('revision') or 1)
        if delta != 1 or int(target.get('revision') or 1) < int(record['revision']):
            report['skipped'].append([key, 'revision counter no longer compatible'])
            continue
        target['revision'] -= delta
        target['revision_snapshot'] = normalize_snapshot(target['revision_snapshot'])
        target['revision_fingerprint'] = fingerprint(target['revision_snapshot'])
        target['incident_false_revision_repaired'] = REVISION
        report['false_revisions'].append(key)
    report.update(before_count=len(current), after_count=len(repaired))
    return repaired, report


def atomic_write(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = Path('data/seen_items.json')
    evidence = json.loads(Path('docs/incident_identity_evidence.json').read_text(encoding='utf-8'))
    repaired, report = plan(at(BEFORE), at(INCIDENT), at(REVISION), json.loads(path.read_text(encoding='utf-8')), evidence)
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in report.items()}))
    if args.apply:
        atomic_write(path, repaired)
        atomic_write(Path('docs/incident_repair_report.json'), report)
