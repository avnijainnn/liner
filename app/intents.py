"""P0 #6: every request the caller made in the call, so Vaada can check each one was handled.

Detected requests:
  appointment      book / reschedule / cancel (from the booking extractor)
  update_email     "change my email to x@y.com"
  update_phone     "my new number is 512 555 0100"
  callback_request "can someone call me back"

Each request must end in a verifiable outcome (a record that changed, a callback
task, a successful transfer). A request with none is an incomplete request.
"""
from __future__ import annotations

import re

EMAIL = r'([\w.+-]+@[\w-]+(?:\.[\w-]+)+)'
UPD_EMAIL = re.compile(r'\b(?:change|update|new|correct|switch|fix)\b[^.?!]{0,40}?\bemail\b[^.?!]{0,25}?' + EMAIL +
                       r'|\bmy\s+new\s+email\s+(?:address\s+)?(?:is|should\s+be)\s+' + EMAIL, re.I)
UPD_PHONE = re.compile(r'\b(?:change|update|new|correct|switch)\b[^.?!]{0,40}?\b(?:number|phone)\b[^.?!]{0,25}?(\+?\d[\d\s().-]{8,}\d)', re.I)
CALLBACK_REQ = re.compile(r'\b(?:call\s+me\s+back|have\s+(?:someone|somebody|the\s+\w+)\s+call\s+me|can\s+(?:someone|somebody|you)\s+call\s+me|'
                          r'give\s+me\s+a\s+call(?:\s+back)?|i\s+need\s+a\s+call\s?back)\b', re.I)


def digits(s) -> str:
    d = ''.join(ch for ch in str(s or '') if ch.isdigit())
    return d[-10:]


def detect(turns: list[dict]) -> list[dict]:
    out = []
    for t in turns:
        if t['role'] != 'caller':
            continue
        m = UPD_EMAIL.search(t['text'])
        if m:
            out.append({'kind': 'update_email', 'field': 'email', 'value': (m.group(1) or m.group(2)).lower().rstrip('.'),
                        'turn': t['i'], 'quote': t['text']})
        m = UPD_PHONE.search(t['text'])
        if m and len(digits(m.group(1))) == 10:
            out.append({'kind': 'update_phone', 'field': 'phone', 'value': digits(m.group(1)), 'turn': t['i'], 'quote': t['text']})
        if CALLBACK_REQ.search(t['text']):
            out.append({'kind': 'callback_request', 'turn': t['i'], 'quote': t['text']})
    # last value wins for each field ("change it to a@b.com... sorry, a@c.com")
    seen, final = set(), []
    for r in reversed(out):
        k = r['kind']
        if k in seen:
            continue
        seen.add(k)
        final.append(r)
    return list(reversed(final))


def same_value(field: str, want, have) -> bool:
    if field == 'email':
        return bool(have) and str(have).strip().lower() == str(want).strip().lower()
    return bool(digits(have)) and digits(have) == digits(want)
