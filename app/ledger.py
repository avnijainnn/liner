"""Problem C, execution correctness: one request should produce exactly one effect.

For every commitment Vaada records an *intent* (what should exist afterwards)
and, on every fresh read, counts the real side effects that answer to it:

    none           nothing happened (an outcome failure, Problem B)
    once           exactly one effect: correct
    duplicate      more than one effect for one request (double booking, two texts)
    landed_despite_failure
                   the tool reported failure (or no tool call was logged) but the
                   effect exists: the agent or a workflow retrying would duplicate it

Effects are matched across *linked identities* (other contact records with the
same phone number), so a second booking under a duplicate contact is still
counted. Effects already claimed by another commitment are not double-counted.
Each reconciliation is appended to ledger_entries; nothing is edited in place.
"""
from __future__ import annotations

from datetime import timedelta

from . import crypto
from .db import Database, dumps, iso, loads, now, parse_iso
from .integrations.highlevel import digits

ACTIVE = {'booked', 'confirmed', 'new', 'showed', 'scheduled'}
WRITE_TOOLS = ('book', 'reschedule', 'cancel')


def intent_key(call_ref: str, kind: str, target: dict) -> str:
    return crypto.sha256_hex(crypto.canonical({'call': call_ref, 'kind': kind, 'target': target}))[:24]


def tool_attempts(tool_calls: list[dict], capability_of) -> tuple[int, str]:
    """(number of write attempts, what the tool last reported: ok | failed | none)."""
    writes = [t for t in tool_calls or [] if capability_of({'name': t.get('name'), 'type': t.get('name')}) in WRITE_TOOLS]
    if not writes:
        return 0, 'none'
    last = writes[-1]
    res = last.get('result')
    failed = last.get('successful') is False or (isinstance(res, dict) and res.get('error')) or res in (None, '')
    return len(writes), 'failed' if failed else 'ok'


def linked_contacts(crm, contact_id: str | None, phone: str | None) -> list[str]:
    ids = [contact_id] if contact_id else []
    try:
        ph = phone or (crm.contact_phone(contact_id) if contact_id and hasattr(crm, 'contact_phone') else None)
        if ph and hasattr(crm, 'contacts_by_phone'):
            ids += [c for c in crm.contacts_by_phone(ph) if c not in ids]
    except Exception:
        pass
    return ids


def claimed_elsewhere(db: Database, client_id: int, finding_id: int) -> set[str]:
    rows = db.all("SELECT promised FROM findings WHERE client_id=? AND type='booking' AND id<>? AND state NOT IN ('superseded','waived')",
                  client_id, finding_id)
    return {loads(r['promised'], {}).get('appointment_id') for r in rows} - {None}


def appointment_effects(records: list[dict], promised: dict, call_start, claimed: set[str], tz: str) -> list[dict]:
    action = promised.get('action') or 'book'
    out = []
    for r in records:
        if r.get('status') not in ACTIVE or not r.get('start_at'):
            continue
        if r.get('id') in claimed and r.get('id') != promised.get('appointment_id'):
            continue
        st = parse_iso(r['start_at'])
        if r.get('created_at') and call_start and parse_iso(r['created_at']) < call_start - timedelta(minutes=10) \
                and r.get('id') != promised.get('appointment_id'):
            continue  # existed before this call: not this request's effect
        if action == 'book':
            want = parse_iso(promised['start_at']) if promised.get('start_at') else None
            if want is None or abs((st - want).total_seconds()) > 3 * 3600:
                continue
        elif action == 'reschedule':
            if st < now() - timedelta(hours=1):
                continue
        else:
            continue
        out.append(r)
    return out


def message_effects(msgs: list[dict], call_start, call_end, window_min: int = 24 * 60) -> list[dict]:
    lo = call_start - timedelta(minutes=1) if call_start else None
    hi = (call_end or call_start or now()) + timedelta(minutes=window_min)
    out = []
    for m in msgs:
        if (m.get('direction') or '').startswith('in') or m.get('channel') != 'sms' or not m.get('sent_at'):
            continue
        t = parse_iso(m['sent_at'])
        if lo and t < lo or t > hi:
            continue
        out.append(m)
    return out


def append(db: Database, f: dict, *, kind: str, key: str, expected: dict, effects: list[dict], attempts: int,
           tool_reported: str, verdict: str, note: str, stage: str):
    db.insert('ledger_entries', agency_id=f['agency_id'], finding_id=f['id'], intent_key=key, kind=kind,
              expected=dumps(expected), effects=dumps([{k: e.get(k) for k in ('id', 'contact_id', 'start_at', 'status', 'sent_at', 'body')}
                                                       for e in effects][:10]),
              attempts=attempts, tool_reported=tool_reported, verdict=verdict, note=note, stage=stage, at=iso(now()))


def raise_execution(db: Database, f: dict, ftype: str, reason: str, effects: list[dict]):
    """Open a Problem-C finding linked to the commitment (once per commitment and type)."""
    key = f"exec:{f['id']}:{ftype}"
    if db.one('SELECT 1 FROM findings WHERE dedupe_key=?', key):
        return None
    t = iso(now())
    text = {'duplicate_effect': 'One request, one side effect',
            'retry_risk': 'A failed tool call must not hide a write that landed'}[ftype]
    return db.insert('findings', agency_id=f['agency_id'], client_id=f['client_id'], call_id=f['call_id'], type=ftype,
                     severity='high', state='open', reason=reason,
                     rule=dumps({'field': 'execution', 'value': ftype, 'text': text, 'commitment': f['id']}),
                     span=f['span'], promised=dumps({'commitment_id': f['id'], 'effects': [e.get('id') for e in effects]}),
                     manifest_version=f['manifest_version'], manifest_sha256=f['manifest_sha256'],
                     signature_key_id=f['signature_key_id'], agent_version=f['agent_version'], agent_changed=f['agent_changed'],
                     dedupe_key=key, appointment_at=f['appointment_at'], created_at=t, updated_at=t)


def resolve_false_success(db: Database, f: dict, retry_id: int | None):
    """The write landed, so 'false success' was wrong: close it, linked to the retry-risk finding."""
    db.run("UPDATE findings SET state='superseded', superseded_by=?, reason=?, updated_at=? "
           "WHERE call_id=? AND type='false_success' AND state='open'",
           retry_id, 'The write landed even though the tool reported failure (see retry risk)', iso(now()), f['call_id'])


def entries(db: Database, finding_id: int):
    rows = db.all('SELECT * FROM ledger_entries WHERE finding_id=? ORDER BY id', finding_id)
    for r in rows:
        r['effects_list'] = loads(r['effects'], [])
        r['expected_obj'] = loads(r['expected'], {})
    return rows


def same_phone(a, b) -> bool:
    return bool(digits(a)) and digits(a) == digits(b)
