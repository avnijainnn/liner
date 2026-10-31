"""Which record answers *this* promise, how sure we are, and which completion stages have their own evidence.

A promise is matched to a record in this order:
  1. by ID: the agent's workflow returned a task / message ID for this call (tool result), or an approved
     recovery action created it;
  2. by content: a callback task whose title or notes are about calling the customer back; a text that is an
     appointment confirmation naming the booked date and time;
  3. nothing else counts. Unrelated tasks and marketing texts are listed as "seen, not counted" with the reason.

Stages are separate on purpose: a task ticked "done" is not evidence that anyone called the customer.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import parse_iso

CB_TITLE = re.compile(r'\b(call\s?-?\s?back|return(?:ing)?\s+(?:the\s+|their\s+)?call|call\s+(?:the\s+)?(?:customer|patient|caller|client)|'
                      r'follow[- ]?up\s+call)\b', re.I)
MARKETING = re.compile(r'(%\s*off|\bspecials?\b|\bpromo|\bsale\b|reply\s+stop|unsubscribe|\bdeals?\b|newsletter|book\s+online)', re.I)
CONFIRMISH = re.compile(r'\b(confirm|appointment|booked|scheduled|reminder)', re.I)
CLOCK = re.compile(r'\b\d{1,2}(:\d\d)?\s?(am|pm|a\.m\.|p\.m\.)', re.I)
REACHED = re.compile(r'\b(reached|answered|spoke|connected|completed)\b', re.I)


def tool_ids(tool_calls: list[dict], pattern: str) -> set[str]:
    rx = re.compile(pattern, re.I)
    out = set()
    for t in tool_calls or []:
        r = t.get('result')
        if rx.search(t.get('name') or '') and isinstance(r, dict) and r.get('id') and t.get('successful') is not False and not r.get('error'):
            out.add(str(r['id']))
    return out


def match_callback(tasks: list[dict], ids: set[str]) -> tuple[dict | None, str | None, list[dict]]:
    by_id = [t for t in tasks if str(t.get('id')) in ids]
    by_title = [t for t in tasks if t not in by_id and CB_TITLE.search(f"{t.get('title') or ''} {t.get('body') or ''}")]
    task = (by_id or by_title or [None])[0]
    link = 'id' if by_id else ('title' if by_title else None)
    ignored = [{'id': t.get('id'), 'label': t.get('title') or '', 'why': 'a second callback task for the same promise' if t in by_id + by_title
                else 'not a callback task for this request'} for t in tasks if t is not task]
    return task, link, ignored


def appt_tokens(start_iso: str | None, tz: str) -> dict | None:
    if not start_iso:
        return None
    d = parse_iso(start_iso).astimezone(ZoneInfo(tz))
    h12 = d.hour % 12 or 12
    ampm = 'am' if d.hour < 12 else 'pm'
    times = [f'{h12}:{d.minute:02d}']
    if not d.minute:
        times += [f'{h12} {ampm}', f'{h12}{ampm}']
    return {'date': [f"{d:%B} {d.day}", f"{d:%b} {d.day}", f'{d.month}/{d.day}'], 'time': times,
            'label': f"{d:%a %b} {d.day}, {h12}:{d.minute:02d} {ampm.upper()}"}


def classify_message(m: dict, tok: dict | None) -> str:
    body = m.get('body') or ''
    low = body.lower()
    if MARKETING.search(body):
        return 'marketing'
    if tok and any(x.lower() in low for x in tok['date']) and any(x.lower() in low for x in tok['time']):
        return 'confirmation'
    if CONFIRMISH.search(body) and CLOCK.search(body):
        return 'other_appointment'
    if CONFIRMISH.search(body):
        return 'generic'
    return 'other'


WHY = {'marketing': 'marketing text, not a confirmation', 'other_appointment': 'confirms a different date or time than the caller agreed',
       'generic': 'doesn’t name the appointment’s date and time', 'other': 'not about this appointment'}


def stage(name: str, state: str, note: str = '') -> dict:
    return {'name': name, 'state': state, 'note': note}


def booking_stages(f: dict, rec: dict | None, read: bool, case: dict | None, sms_state: str | None) -> list[dict]:
    told = ('done' if case and case.get('customer_contact') == 'contacted' else
            'missing' if case and case.get('status') == 'fixed_awaiting_contact' else
            'done' if sms_state == 'verified' and f['state'] == 'verified' else
            'missing' if sms_state == 'contradicted' else
            'unknown' if f['state'] == 'verified' else 'waiting')
    return [stage('Record exists', 'done' if rec else ('missing' if read else 'waiting'), f"{rec.get('id')}, {rec.get('status')}" if rec else ''),
            stage('Details match', 'done' if f['state'] == 'verified' else ('missing' if f['state'] == 'contradicted' and rec else 'waiting')),
            stage('Customer told', told, {'done': 'confirmation verified or customer marked contacted',
                                          'missing': 'the customer hasn’t been told the correct details'}.get(told, ''))]


LABEL = {'done': 'done', 'missing': 'missing', 'waiting': 'not yet', 'unproven': 'not proven', 'unknown': 'no evidence'}
