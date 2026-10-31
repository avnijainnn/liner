"""Problem A, semantic correctness: is what the agent *said* true for this business?

The manifest carries business facts the client approved (prices, refund
window, cancellation notice and fee, insurance, hours). This module extracts
factual claims from agent sentences and compares them deterministically with
those facts. Rules:
  * no approved fact -> no verdict (the claim is "unverified", never "wrong")
  * a claim made right after the agent's own knowledge lookup failed, and not
    confirmed by an approved fact, is flagged as an ungrounded answer
  * quotes are kept verbatim so the operator sees exactly what was said
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .labeler import sentences

AMOUNT = r'\$\s?(\d{1,5}(?:\.\d\d)?)|(\d{1,5})\s+(?:dollars|bucks)'
DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
DAY_WORDS = {'monday': 'mon', 'tuesday': 'tue', 'wednesday': 'wed', 'thursday': 'thu', 'friday': 'fri',
             'saturday': 'sat', 'sunday': 'sun', 'saturdays': 'sat', 'sundays': 'sun', 'mondays': 'mon',
             'tuesdays': 'tue', 'wednesdays': 'wed', 'thursdays': 'thu', 'fridays': 'fri'}
LOOKUP_TOOL = re.compile(r'lookup|search|knowledge|\bkb\b|faq|policy|get_info|retriev', re.I)
FACTY = re.compile(r'\$\s?\d|\d+\s*(?:days?|hours?|percent|%)|\bpolicy\b|\bwe\s+(?:do|don\'?t|do\s+not|offer|accept|take|charge|are\s+open|\'re\s+open)\b|\bit\s+(?:costs|is\s+\$)', re.I)
QUESTION_ONLY = re.compile(r'\?\s*$')

DEFAULT_FACTS = {'prices': {}, 'refund_window_days': None, 'cancellation_notice_hours': None,
                 'cancellation_fee': None, 'accepts_insurance': None}


def validate_facts(f: dict | None) -> dict:
    f = dict(DEFAULT_FACTS, **(f or {}))
    prices = {}
    for k, v in (f.get('prices') or {}).items():
        key = re.sub(r'[^a-z0-9 ]+', '', str(k).lower()).strip()
        if not key:
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            raise ValueError(f'Price for {k!r} must be a number')
        if val < 0:
            raise ValueError('Prices cannot be negative')
        prices[key] = int(val) if val == int(val) else round(val, 2)
    out = {'prices': dict(sorted(prices.items()))}
    for k in ('refund_window_days', 'cancellation_notice_hours', 'cancellation_fee'):
        v = f.get(k)
        if v in (None, ''):
            out[k] = None
        else:
            try:
                out[k] = int(float(v))
            except (TypeError, ValueError):
                raise ValueError(f'{k} must be a whole number')
            if out[k] < 0:
                raise ValueError(f'{k} cannot be negative')
    ai = f.get('accepts_insurance')
    out['accepts_insurance'] = None if ai in (None, '', 'unknown') else (ai in (True, 'true', 'yes', '1', 1))
    return out


def _amount(m):
    return float(m.group(1) or m.group(2))


def _service_aliases(name: str):
    base = re.escape(name)
    return re.compile(rf'\b{base}(?:s|es)?\b', re.I)


def claims(sentence: str, facts: dict) -> list[dict]:
    """Factual claims in one agent sentence: [{key, said, label}]."""
    s = sentence
    out = []
    if QUESTION_ONLY.search(s.strip()):
        return out
    # prices: pair each mentioned service with the nearest amount after it (or before it)
    amounts = [(m.start(), _amount(m)) for m in re.finditer(AMOUNT, s, re.I)]
    if amounts:
        for svc in (facts.get('prices') or {}):
            for m in _service_aliases(svc).finditer(s):
                after = [a for a in amounts if a[0] >= m.end()]
                before = [a for a in amounts if a[0] < m.start()]
                pick = (after[0] if after else (before[-1] if before else None))
                if pick and not re.search(r'cancell?ation\s+fee|no[- ]show', s[min(m.start(), pick[0]):max(m.end(), pick[0]) + 12], re.I):
                    out.append({'key': f'prices.{svc}', 'said': pick[1], 'label': f'{svc} price'})
                    break
    m = re.search(r'(\d+)[- ]days?\b[^.]{0,40}\brefund|\brefund[^.]{0,50}?\b(?:within|in|for|up\s+to)\s+(\d+)\s+days', s, re.I)
    if m:
        out.append({'key': 'refund_window_days', 'said': int(m.group(1) or m.group(2)), 'label': 'refund window'})
    m = re.search(r'(\d+)\s*(hours?|days?)\b[^.]{0,25}\b(?:notice|in\s+advance|ahead|before)', s, re.I)
    if m and re.search(r'cancel|reschedul', s, re.I):
        n = int(m.group(1)) * (24 if m.group(2).lower().startswith('day') else 1)
        out.append({'key': 'cancellation_notice_hours', 'said': n, 'label': 'cancellation notice'})
    m = re.search(r'cancell?ation\s+fee[^.$\d]{0,20}(?:' + AMOUNT + r')|(?:' + AMOUNT + r')\s+(?:cancell?ation|late[- ]cancel)', s, re.I)
    if m:
        vals = [g for g in m.groups() if g]
        out.append({'key': 'cancellation_fee', 'said': float(vals[0]), 'label': 'cancellation fee'})
    elif re.search(r'\bno\s+cancell?ation\s+fee\b|\bfree\s+to\s+cancel\b', s, re.I):
        out.append({'key': 'cancellation_fee', 'said': 0.0, 'label': 'cancellation fee'})
    m = re.search(r"\bwe\s+(do\s+not|don'?t|do)?\s*(?:accept|take)\s+(?:most\s+|all\s+)?insurance", s, re.I)
    if m:
        out.append({'key': 'accepts_insurance', 'said': not bool(m.group(1) and m.group(1).lower() != 'do'), 'label': 'insurance'})
    m = re.search(r"\b(?:we'?re|we\s+are)\s+(?:open|here)\s+(?:on\s+)?(saturdays?|sundays?|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|weekends)\b", s, re.I)
    if m:
        w = m.group(1).lower()
        for d in (['sat', 'sun'] if w == 'weekends' else [DAY_WORDS[w]]):
            out.append({'key': f'business_hours.{d}', 'said': 'open', 'label': f'open on {d}'})
    m = re.search(r"\b(?:we'?re|we\s+are)\s+closed\s+(?:on\s+)?(saturdays?|sundays?|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?)\b", s, re.I)
    if m:
        out.append({'key': f'business_hours.{DAY_WORDS[m.group(1).lower()]}', 'said': 'closed', 'label': 'closed day'})
    m = re.search(r"\bopen\s+(?:until|till|to)\s+(\d{1,2})(?::(\d\d))?\s*(am|pm|a\.m\.|p\.m\.)", s, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower().startswith('p') else 0)
        out.append({'key': 'business_hours.close', 'said': f'{h:02d}:{m.group(2) or "00"}', 'label': 'closing time'})
    return out


def _fact_value(key: str, facts: dict, body: dict, call_day: str | None):
    if key.startswith('prices.'):
        return (facts.get('prices') or {}).get(key[7:])
    if key.startswith('business_hours.'):
        bh = body.get('business_hours') or {}
        d = key.split('.', 1)[1]
        if d == 'close':
            win = bh.get(call_day) if call_day else None
            if not win:
                closes = [w[1] for w in bh.values() if w]
                win = [None, max(set(closes), key=closes.count)] if closes else None
            return win[1] if win else None
        return 'open' if bh.get(d) else 'closed'
    return facts.get(key)


def _fmt(key, v):
    if v is None:
        return 'not set'
    if key.startswith('prices.') or key == 'cancellation_fee':
        return f'${v:g}' if isinstance(v, (int, float)) else str(v)
    if key == 'refund_window_days':
        return f'{v} days'
    if key == 'cancellation_notice_hours':
        return f'{v} hours'
    if key == 'accepts_insurance':
        return 'accepts insurance' if v else 'does not accept insurance'
    return str(v)


def check(body: dict, turns: list[dict], tools: list[dict], started_at: datetime | None) -> dict:
    """Returns {'findings': [...], 'verified': [...], 'unverified': [...]}."""
    facts = body.get('facts') or {}
    call_day = None
    if started_at is not None:
        call_day = DAYS[started_at.astimezone(ZoneInfo(body.get('timezone') or 'UTC')).weekday()]
    failed_after = [t.get('after_turn', -1) for t in tools or [] if LOOKUP_TOOL.search(t.get('name') or '') and
                    (t.get('successful') is False or (isinstance(t.get('result'), dict) and (t['result'].get('error') or t['result'].get('results') == []))
                     or t.get('result') in (None, '', [], {}))]
    first_fail = min(failed_after) if failed_after else None
    findings, verified, unverified = [], [], []
    seen = set()
    for t in turns:
        if t['role'] != 'agent':
            continue
        for off, s in sentences(t['text']):
            cs = claims(s, facts)
            after_fail = first_fail is not None and t['i'] > first_fail
            for c in cs:
                fact = _fact_value(c['key'], facts, body, call_day)
                span = {'turn': t['i'], 'start': off, 'end': off + len(s), 'quote': s}
                if fact is None:
                    unverified.append({**c, 'span': span})
                    continue
                said = c['said']
                same = (abs(float(said) - float(fact)) < 0.01) if isinstance(fact, (int, float)) and not isinstance(fact, bool) else said == fact
                if same:
                    verified.append({**c, 'fact': fact, 'span': span})
                    continue
                k = ('wrong_information', c['key'])
                if k in seen:
                    continue
                seen.add(k)
                findings.append({'type': 'wrong_information', 'severity': 'high', 'span': span,
                                 'rule': {'field': f'facts.{c["key"]}' if not c['key'].startswith('business_hours') else c['key'],
                                          'value': c['key'], 'approved': fact,
                                          'text': f'Approved {c["label"]}: {_fmt(c["key"], fact)}'},
                                 'reason': f'Agent said the {c["label"]} is {_fmt(c["key"], said)}; the approved fact is {_fmt(c["key"], fact)}'})
            # Ungrounded: an answer right after the agent's own lookup failed, not backed by an approved fact.
            if after_fail and FACTY.search(s) and not QUESTION_ONLY.search(s.strip()):
                backed = cs and all(_fact_value(c['key'], facts, body, call_day) is not None and
                                    not any(f['span']['quote'] == s for f in findings) for c in cs)
                if not backed and ('ungrounded_answer', 0) not in seen:
                    seen.add(('ungrounded_answer', 0))
                    findings.append({'type': 'ungrounded_answer', 'severity': 'high',
                                     'span': {'turn': t['i'], 'start': off, 'end': off + len(s), 'quote': s},
                                     'rule': {'field': 'facts', 'value': 'grounding',
                                              'text': 'Answers must come from an approved fact or a successful lookup'},
                                     'reason': "The agent's knowledge lookup failed, then it answered anyway with a claim no approved fact supports"})
    return {'findings': findings, 'verified': verified, 'unverified': unverified}


def fact_value(key: str, body: dict):
    return _fact_value(key, body.get('facts') or {}, body, None)


def same(said, fact) -> bool:
    if isinstance(fact, bool) or isinstance(said, bool):
        return said == fact
    try:
        return abs(float(said) - float(fact)) < 0.01
    except (TypeError, ValueError):
        return said == fact


fmt = _fmt


def claims_in_text(text: str, facts: dict) -> list[dict]:
    out = []
    for _, s in sentences(text):
        for c in claims(s, facts):
            out.append({**c, 'quote': s})
    return out
