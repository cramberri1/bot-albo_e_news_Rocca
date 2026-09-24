"""Identity and semantic comparisons; no I/O or Telegram side effects."""
import hashlib
import html
import json
import re
import unicodedata


def normalize_field(value):
    value = html.unescape(str(value or ''))
    value = re.sub(r'<[^>]*>', ' ', value)
    value = unicodedata.normalize('NFKC', value)
    value = ''.join(c for c in value if unicodedata.category(c) != 'Cf')
    return ' '.join(value.split()).casefold()


def normalize_number(value):
    value = normalize_field(value)
    if value in {'', '-', '–', '—', '0', 'del', 'n/d', 'n.d.', 'nessuno'}:
        return ''
    if not re.fullmatch(r'[\w./-]+', value) or not re.search(r'\d', value):
        return ''
    return (str(int(value)) if int(value) else '') if value.isdecimal() else value


def parse_publication_number(value):
    text = normalize_field(value)
    match = re.search(r'\bpubblicazione\s*(?:n(?:umero)?\.?|numero|:)\s*([\w./-]+)', text)
    return normalize_number(match.group(1) if match else text)


def parse_act_number(value):
    match = re.search(r'\batto\s+n\.?\s*([\w./-]+)', normalize_field(value))
    return normalize_number(match.group(1)) if match else ''


def parse_register_number(value):
    match = re.search(r'\bregistro\s+generale\s+n\.?\s*([\w./-]+)', normalize_field(value))
    return normalize_number(match.group(1)) if match else ''


def legacy_item_id(item):
    raw = '|'.join(str(item.get(k, '')) for k in ('title', 'num_pub', 'tipo'))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def item_id_v2(item):
    number = parse_publication_number(item.get('num_pub'))
    date = normalize_field(item.get('date'))
    year = date[-4:] if re.fullmatch(r'\d{2}-\d{2}-\d{4}', date) else ''
    # Publication numbers are annual and scoped to this municipality.
    if number and year:
        key = ['e1396', year, number]
    else:
        key = ['e1396', date, *[normalize_field(item.get(k)) for k in ('title', 'tipo', 'sender')]]
    return 'v2:' + hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24]


def normalize_snapshot(snapshot):
    snapshot = snapshot or {}
    result = {k: normalize_field(snapshot.get(k)) for k in ('date', 'date_end', 'sender')}
    result.update({k: normalize_number(snapshot.get(k)) for k in ('act_number', 'register_number')})
    result['attachment_count'] = int(snapshot.get('attachment_count') or 0)
    for k in ('attachment_sha256', 'attachment_name_hashes'):
        result[k] = sorted(str(v).casefold() for v in snapshot.get(k, []))
    return result


def find_existing_equivalent_item(item, db, snapshot=None):
    """Return only an unambiguous match; never match solely by date or sender."""
    ids = {item_id_v2(item), item.get('_legacy_id', legacy_item_id(item))}
    def compatible_publication(record):
        old_date = str(record.get('date') or '')
        new_date = str(item.get('date') or '')
        if record.get('num_pub') and old_date and new_date:
            # The old hash omitted the publication year; don't inherit that collision.
            return old_date[-4:] == new_date[-4:]
        return True
    direct = [h for h, r in db.items() if compatible_publication(r) and
              (h in ids or ids.intersection(r.get('legacy_ids', [])) or r.get('primary_id') in ids)]
    if len(direct) == 1:
        return direct[0]
    if direct:
        return None
    if snapshot is None:
        return None
    current = normalize_snapshot(snapshot)
    if not current['attachment_sha256'] or not current['date'] or not current['sender']:
        return None
    matches = []
    for h, record in db.items():
        old = normalize_snapshot(record.get('revision_snapshot'))
        if current != old:
            continue
        # A different explicit publication is a republication, even with identical files.
        if record.get('num_pub') and parse_publication_number(record['num_pub']) != parse_publication_number(item.get('num_pub')):
            continue
        if record.get('title') and normalize_field(record['title']) != normalize_field(item.get('title')):
            continue
        matches.append(h)
    return matches[0] if len(matches) == 1 else None


def flood_reason(items, candidate_new, *, max_new=30, ratio=.65, ratio_min=15, old_count=5, max_age=30, today):
    old = sum(1 for i in candidate_new if _age(i, today) is None or _age(i, today) > max_age)
    if len(candidate_new) > max_new or (len(candidate_new) >= ratio_min and len(candidate_new) / max(1, len(items)) >= ratio) or old >= old_count:
        return f'SAFETY STOP: rilevati {len(candidate_new)} presunti nuovi atti su {len(items)} online ({old} storici/data ignota). Possibile modifica del portale/parser. Notifiche sospese.'
    return ''


def _age(item, today):
    from datetime import datetime
    try:
        return (today - datetime.strptime(item.get('date', ''), '%d-%m-%Y').date()).days
    except (ValueError, TypeError):
        return None
