"""Import phone notes from a CSV export (for example a Google Form) into the notebook.

Stopgap for P1.4 until LAN phone access with a PIN exists. Each row becomes a saved
note whose event time is the row's timestamp, recorded as a user estimate with a
stated uncertainty (a form is usually submitted shortly after the moment it
describes). The original timestamp text is kept in the note's "when" input.
Re-importing the same export saves nothing new: the event ID is derived from the
row's content.
"""
from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import io
import re
from zoneinfo import ZoneInfo

MAX_BYTES = 1_000_000
MAX_ROWS = 2000
DEFAULT_UNCERTAINTY_MS = 120_000
TIME_FORMATS = ('%m/%d/%Y %H:%M:%S', '%m/%d/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S',
                '%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y %I:%M %p')
COLUMNS = {
    'time': ('timestamp', 'time', 'when', 'date'),
    'text': ('what happened', 'observation', 'note', 'notes', 'comment', 'description', 'issue'),
    'author': ('your name', 'name', 'observer', 'student', 'who'),
    'tag': ('subsystem', 'mechanism', 'area', 'part', 'category'),
}


class ImportError_(ValueError):
    pass


def _pick(headers, role):
    lowered = [h.strip().lower() for h in headers]
    for wanted in COLUMNS[role]:
        for index, header in enumerate(lowered):
            if header == wanted:
                return index
    for wanted in COLUMNS[role]:
        for index, header in enumerate(lowered):
            if wanted in header:
                return index
    return None


def parse_time(text, zone):
    text = text.strip()
    try:
        value = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        return value if value.tzinfo else value.replace(tzinfo=zone)
    except ValueError:
        pass
    for pattern in TIME_FORMATS:
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=zone)
        except ValueError:
            continue
    raise ImportError_(f'Unrecognized time: {text[:40]}')


def _iso(value):
    text = value.isoformat(timespec='seconds')
    return text.replace('+00:00', 'Z') if text.endswith('+00:00') else text


def payloads(csv_text, *, timezone_name, source_label='csv_import'):
    """(payloads, skipped rows with reasons, column mapping)."""
    if len(csv_text.encode('utf-8')) > MAX_BYTES:
        raise ImportError_('The file is larger than 1 MB')
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        raise ImportError_('Unknown time zone') from None
    rows = list(csv.reader(io.StringIO(csv_text.lstrip('﻿'))))
    if not rows:
        raise ImportError_('The file is empty')
    headers, body = rows[0], rows[1:MAX_ROWS + 1]
    columns = {role: _pick(headers, role) for role in COLUMNS}
    if columns['time'] is None or columns['text'] is None:
        raise ImportError_('Need a timestamp column and a note/observation column')
    out, skipped = [], []
    for number, row in enumerate(body, start=2):
        def cell(role):
            index = columns[role]
            return row[index].strip() if index is not None and index < len(row) else ''
        text = cell('text')
        if not text:
            skipped.append({'row': number, 'reason': 'empty note'})
            continue
        try:
            moment = parse_time(cell('time'), zone)
        except ImportError_ as exc:
            skipped.append({'row': number, 'reason': str(exc)})
            continue
        iso = _iso(moment)
        utc_ns = str(int(moment.timestamp()) * 1_000_000_000)
        digest = hashlib.sha256('\x1f'.join([source_label, iso, cell('author'), cell('tag'), text]).encode()).hexdigest()[:40]
        tag = re.sub(r'[^a-z0-9 _-]', '', cell('tag').lower())[:64]
        payload = {'schema_version': 1, 'event_id': 'csv-' + digest, 'revision': 1,
                   'submitted_utc_ns': utc_ns, 'client_monotonic_ns': None,
                   'event_utc_start_ns': utc_ns, 'event_utc_end_ns': utc_ns,
                   'when': {'kind': 'exact_interval', 'start': iso, 'end': iso},
                   'uncertainty_ms': DEFAULT_UNCERTAINTY_MS, 'clock_domain': 'csv_form_timestamp:' + timezone_name,
                   'clock_quality': 'user_estimate', 'text': text[:4000], 'tags': [tag] if tag else [],
                   'source': source_label, 'author': cell('author')[:64] or 'unspecified'}
        out.append(payload)
    mapping = {role: (headers[index] if index is not None else None) for role, index in columns.items()}
    return out, skipped, mapping


def import_csv(notebook, csv_text, *, timezone_name, source_label='csv_import'):
    from .notebook import NotebookError
    items, skipped, mapping = payloads(csv_text, timezone_name=timezone_name, source_label=source_label)
    saved = existing = 0
    for payload in items:
        try:
            result = notebook.save(payload)
        except NotebookError as exc:
            skipped.append({'row': None, 'reason': str(exc)[:120]})
            continue
        if result['idempotent']:
            existing += 1
        else:
            saved += 1
    return {'saved': saved, 'already_imported': existing, 'skipped': skipped, 'columns': mapping}
