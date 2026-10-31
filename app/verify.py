"""Outcome verification against fresh record reads (bookings and promised texts),
with the execution ledger counting side effects per commitment.

States: pending -> verified | contradicted | unknown, plus superseded and waived.
Only a fresh read that matches contact, calendar, start time and status can
verify. Every read is stored as an immutable observation.
"""
from __future__ import annotations

from datetime import timedelta

from .db import Database, dumps, iso, loads, now, parse_iso
from .integrations.highlevel import CRMError, crm_for

ACTIVE = {'booked', 'confirmed', 'new', 'showed', 'scheduled'}
CANCELLED = {'cancelled', 'canceled', 'noshow', 'invalid'}
FINAL_STATES = {'verified', 'superseded', 'waived'}


def read_schedule(call_end, appt_at, reads_min: list[int]) -> list[tuple[str, object]]:
    """2 / 15 / 60 minutes after call end; compressed when the appointment is close."""
    out, seen = [], set()
    for m in reads_min:
        at = call_end + timedelta(minutes=m)
        if appt_at is not None and at > appt_at - timedelta(minutes=10):
            at = max(call_end + timedelta(minutes=1), appt_at - timedelta(minutes=10))
        key = at.replace(second=0, microsecond=0)
        if key in seen:
            continue
        seen.add(key)
        out.append((f'{m}m', at))
    return out


def schedule_reads(db: Database, finding: dict, call_end, appt_at, reads_min):
    plan = read_schedule(call_end, appt_at, reads_min)
    for i, (stage, at) in enumerate(plan):
        st = 'final' if i == len(plan) - 1 else stage
        db.insert('jobs', agency_id=finding['agency_id'], kind='recheck', ref_id=finding['id'], stage=st, run_at=iso(at))
    return plan


def compare(promised: dict, rec: dict | None, calendars: list[str]) -> tuple[bool, str, bool]:
    """(match, reason, unknown_fields)."""
    action = promised.get('action') or 'book'
    if rec is None:
        return False, 'Appointment not found in the calendar', False
    if action == 'cancel':
        st = (rec.get('status') or '').lower()
        if st in CANCELLED:
            return True, 'Appointment is cancelled in the calendar', False
        if not st:
            return False, 'Calendar record has no status', True
        return False, f'Appointment is still {st} in the calendar', False
    missing = [k for k in ('contact_id', 'calendar_id', 'start_at', 'status') if not rec.get(k)]
    if missing:
        return False, f"Calendar record is missing {', '.join(missing)}", True
    if promised.get('contact_id') and rec['contact_id'] != promised['contact_id']:
        return False, 'Appointment belongs to a different contact', False
    if calendars and rec['calendar_id'] not in calendars:
        return False, 'Appointment is on a calendar outside this manifest', False
    if rec['status'] in CANCELLED:
        return False, f"Appointment status is {rec['status']}", False
    if rec['status'] not in ACTIVE:
        return False, f"Appointment status '{rec['status']}' is not a confirmed booking", True
    if parse_iso(rec['start_at']) != parse_iso(promised['start_at']):
        return False, 'Start time differs from what the caller finally agreed', False
    return True, 'Fresh read matches contact, calendar, start time and status', False


def check(db: Database, finding_id: int, stage: str, actor: str = 'vaada') -> dict:
    f = db.one('SELECT * FROM findings WHERE id=?', finding_id)
    if not f or f['type'] not in ('booking', 'message_promised', 'callback_promised', 'contact_update'):
        raise ValueError('Only record findings are re-checked against records')
    if f['state'] in ('superseded', 'waived'):
        return f
    if f['type'] == 'message_promised':
        return check_message(db, f, stage, actor)
    if f['type'] == 'callback_promised':
        return check_callback(db, f, stage, actor)
    if f['type'] == 'contact_update':
        return check_contact(db, f, stage, actor)
    from . import ledger
    from .manifest import tool_capability
    client = db.one('SELECT * FROM clients WHERE id=?', f['client_id'])
    call = db.one('SELECT * FROM calls WHERE id=?', f['call_id'])
    p = f.j('promised', {})
    calendars = p.get('calendars') or []
    records, err, err_kind, ident_ok, linked = [], None, None, True, []
    try:
        crm = crm_for(db, client)
        if p.get('contact_id'):
            ident_ok = crm.contact_in_location(p['contact_id'])
        if p.get('appointment_id'):
            r = crm.appointment(p['appointment_id'])
            if r:
                records.append(r)
        linked = ledger.linked_contacts(crm, p.get('contact_id'), p.get('caller_phone'))
        for cid in linked:
            for r in crm.contact_appointments(cid):
                if r['id'] not in {x['id'] for x in records}:
                    records.append(r)
    except CRMError as e:
        err, err_kind = str(e), e.kind
    except Exception as e:  # never let an adapter bug verify anything
        err, err_kind = f'unexpected read error: {type(e).__name__}', 'error'

    final = stage in ('final', 'manual_final')
    prev = f['state']
    attempts, reported = ledger.tool_attempts(loads(call['tool_calls'], []), tool_capability)
    effects, lverdict, lnote = [], None, ''
    if err:
        verdict, reason = 'error', err
        if err_kind == 'auth':
            db.update('clients', {'id': client['id']}, connection_status='error', connection_error=err[:200],
                      connection_checked_at=iso(now()))
        new_state = 'unknown' if final or stage == 'manual' else 'pending'
    elif not ident_ok:
        verdict, reason, new_state = 'identity', 'Contact is not demonstrably in this sub-account', 'unknown'
    elif not p.get('contact_id') and not p.get('appointment_id'):
        verdict, reason, new_state = 'identity', 'No contact or appointment ID to check against', 'unknown'
    else:
        primary = None
        if p.get('appointment_id'):
            primary = next((r for r in records if r['id'] == p['appointment_id']), None)
        own = [r for r in records if r.get('contact_id') == p.get('contact_id')] or records
        cands = [primary] if primary else own
        results = [(r, *compare(p, r, calendars)) for r in cands] or [(None, *compare(p, None, calendars))]
        matches = [r for r, ok, _, _ in results if ok]
        if p.get('action') in ('book', 'reschedule'):
            effects = ledger.appointment_effects(records, p, parse_iso(call['started_at']),
                                                 ledger.claimed_elsewhere(db, f['client_id'], finding_id), client['timezone'])
        if matches and len(effects) > 1:
            contacts = {e.get('contact_id') for e in effects}
            verdict = lverdict = 'duplicate'
            reason = (f'{len(effects)} live appointments answer one request'
                      + (f' (under {len(contacts)} contact records with the same phone)' if len(contacts) > 1 else '')
                      + (f'; the booking tool was called {attempts} times' if attempts > 1 else ''))
            new_state = 'contradicted' if final or prev == 'contradicted' or stage == 'manual' else 'pending'
        elif matches:
            verdict, reason, new_state = 'match', results[[r for r, *_ in results].index(matches[0])][2], 'verified'
            lverdict = 'landed_despite_failure' if reported in ('failed', 'none') and p.get('action') != 'cancel' else 'once'
            if attempts > 1 and lverdict == 'once':
                lnote = f'Booking tool called {attempts} times; exactly one appointment exists (the retry was harmless)'
        else:
            unknown_fields = any(u for _, _, _, u in results)
            reason = results[0][2]
            verdict = 'mismatch'
            lverdict = 'none' if not effects else ('duplicate' if len(effects) > 1 else 'mismatch')
            if len(effects) > 1:
                reason += f'; {len(effects)} other live appointments exist for this caller'
            if unknown_fields:
                new_state = 'unknown' if final else 'pending'
            elif final or prev == 'contradicted' or (stage == 'manual' and _past_final(db, finding_id)):
                new_state = 'unknown' if p.get('uncertain') else 'contradicted'
                if p.get('uncertain'):
                    reason += ' (caller time was inferred, so this stays unknown)'
            else:
                new_state = 'pending'
    fields = {'records': records[:10], 'linked_contacts': linked,
              'promised': {k: p.get(k) for k in ('action', 'start_at', 'contact_id', 'appointment_id')}}
    db.insert('observations', agency_id=f['agency_id'], finding_id=finding_id, appointment_id=p.get('appointment_id'),
              fields=dumps(fields), verdict=verdict, reason=reason, stage=stage,
              source='sandbox' if client['is_sandbox'] else 'highlevel', read_at=iso(now()))
    if lverdict:
        key = ledger.intent_key(call['retell_call_id'], p.get('action') or 'book',
                                {'contact': p.get('contact_id'), 'start': p.get('start_at'), 'appt': p.get('appointment_id')})
        ledger.append(db, f, kind='appointment', key=key,
                      expected={'action': p.get('action'), 'start_at': p.get('start_at'), 'contact_id': p.get('contact_id'), 'count': 1},
                      effects=effects, attempts=attempts, tool_reported=reported, verdict=lverdict, note=lnote, stage=stage)
        if lverdict == 'duplicate' and new_state == 'contradicted':
            ledger.raise_execution(db, f, 'duplicate_effect', reason, effects)
        if lverdict == 'landed_despite_failure':
            rid = ledger.raise_execution(
                db, f, 'retry_risk',
                ('The booking tool reported failure' if reported == 'failed' else 'No booking tool call was logged')
                + ', but the appointment exists. If the agent or a workflow retries, the caller will be booked twice.', effects)
            ledger.resolve_false_success(db, f, rid)
    db.update('findings', {'id': finding_id}, state=new_state, reason=reason, updated_at=iso(now()))
    if new_state == 'verified':
        db.run('UPDATE jobs SET done=1 WHERE kind=? AND ref_id=? AND done=0', 'recheck', finding_id)
    from . import cases
    cases.on_finding_state(db, finding_id, prev, new_state, actor=actor)
    return db.one('SELECT * FROM findings WHERE id=?', finding_id)


def check_message(db: Database, f: dict, stage: str, actor: str = 'vaada') -> dict:
    """A promised text confirmation: did exactly one outbound SMS reach the caller after the call?"""
    from . import ledger
    client = db.one('SELECT * FROM clients WHERE id=?', f['client_id'])
    call = db.one('SELECT * FROM calls WHERE id=?', f['call_id'])
    p = f.j('promised', {})
    prev, final = f['state'], stage in ('final',)
    msgs, err, linked = [], None, []
    try:
        crm = crm_for(db, client)
        contact = p.get('contact_id')
        if not contact and p.get('caller_phone') and hasattr(crm, 'contacts_by_phone'):
            found = crm.contacts_by_phone(p['caller_phone'])
            contact = found[0] if found else None
        linked = ledger.linked_contacts(crm, contact, p.get('caller_phone'))
        for cid in linked:
            msgs += crm.messages(cid)
    except CRMError as e:
        err = str(e)
    except Exception as e:
        err = f'unexpected read error: {type(e).__name__}'
    from . import evidence
    window = ledger.message_effects(msgs, parse_iso(call['started_at']), parse_iso(call['ended_at']))
    book = db.one("SELECT promised FROM findings WHERE call_id=? AND type='booking' ORDER BY id DESC", f['call_id'])
    appt = (loads(book['promised'], {}) or {}).get('start_at') if book else None
    tok = evidence.appt_tokens(appt, client['timezone'])
    ids = evidence.tool_ids(loads(call['tool_calls'], []), r'sms|text|message') | set(p.get('recovery_ids') or [])
    rows = [(m, evidence.classify_message(m, tok)) for m in window]
    effects = [m for m, k in rows if k == 'confirmation']
    link = 'id' if any(str(m.get('id')) in ids for m in effects) else ('content' if effects else None)
    ignored = [{'id': m.get('id'), 'label': (m.get('body') or '')[:120], 'why': evidence.WHY[k]} for m, k in rows if k != 'confirmation']
    kinds = {k for _, k in rows}
    if err:
        state, reason, lv = ('unknown' if final or stage == 'manual' else 'pending'), err, None
    elif not linked:
        state, reason, lv = 'unknown', 'Could not identify the caller in the CRM, so the text cannot be checked', None
    elif effects and all((m.get('status') or '') == 'failed' for m in effects):
        state, reason, lv = 'contradicted', 'The confirmation was written, but the provider reports it failed', 'none'
    elif len(effects) == 1:
        state, reason, lv = 'verified', f"One confirmation naming {tok['label']} was sent ({'linked by ID' if link == 'id' else 'matched by content'})", 'once'
    elif len(effects) > 1:
        state, reason, lv = 'verified', f'The confirmation was sent {len(effects)} times', 'duplicate'
    elif not tok and 'generic' in kinds:
        state, reason, lv = 'unknown', 'A confirmation-style text was sent, but there is no booked time on this call to check it against', None
    else:
        lv = 'none'
        if final or prev == 'contradicted' or (stage == 'manual' and _past_final(db, f['id'])):
            first = next((m for m, k in rows if k == 'other_appointment'), None) or next((m for m, k in rows if k == 'marketing'), None)
            state, reason = 'contradicted', ('The confirmation text names a different time than the caller agreed' if 'other_appointment' in kinds else
                                             f"Only a marketing text was sent (“{(first.get('body') or '')[:60]}…”); no confirmation for the appointment" if 'marketing' in kinds else
                                             'A text went out, but it doesn’t name the appointment’s date and time' if 'generic' in kinds else
                                             'The agent promised a text confirmation and none was sent')
        else:
            state, reason = 'pending', 'No confirmation yet; checking again later'
    g = effects[0] if effects else None
    late = state in ('contradicted', 'unknown')
    stages = [evidence.stage('Confirmation written', 'done' if g else ('missing' if late else 'waiting'),
                             f"{g.get('id')}, “{(g.get('body') or '')[:90]}” ({'linked by ID' if link == 'id' else 'matched by content'})" if g else ''),
              evidence.stage('Names the right appointment', 'done' if g else ('missing' if late and rows else 'waiting'),
                             f"expects {tok['label']}" if tok else 'no booked time on this call to check against'),
              evidence.stage('Sent', 'done' if g and (g.get('status') or 'sent') != 'failed' else ('missing' if g else 'waiting')),
              evidence.stage('Delivered', 'done' if g and g.get('status') == 'delivered' else ('missing' if g and g.get('status') == 'failed' else
                             ('unknown' if g else 'waiting')), 'carrier delivery receipt' if g and g.get('status') == 'delivered' else
                             ('no delivery receipt from the provider' if g else ''))]
    db.insert('observations', agency_id=f['agency_id'], finding_id=f['id'], appointment_id=None,
              fields=dumps({'messages': effects[:10], 'linked_contacts': linked, 'stages': stages, 'ignored': ignored[:5], 'link': link}), verdict=lv or 'error', reason=reason, stage=stage,
              source='sandbox' if client['is_sandbox'] else 'highlevel', read_at=iso(now()))
    if lv:
        key = ledger.intent_key(call['retell_call_id'], 'sms', {'contact': p.get('contact_id'), 'phone': p.get('caller_phone')})
        ledger.append(db, f, kind='message', key=key, expected={'channel': 'sms', 'count': 1, 'to': p.get('caller_phone') or p.get('contact_id')},
                      effects=effects, attempts=0, tool_reported='n/a', verdict=lv, note='', stage=stage)
        if lv == 'duplicate':
            ledger.raise_execution(db, f, 'duplicate_effect', reason + ' for one promise.', effects)
    db.update('findings', {'id': f['id']}, state=state, reason=reason, updated_at=iso(now()))
    if state == 'verified':
        db.run('UPDATE jobs SET done=1 WHERE kind=? AND ref_id=? AND done=0', 'recheck', f['id'])
    from . import cases
    cases.on_finding_state(db, f['id'], prev, state, actor=actor)
    return db.one('SELECT * FROM findings WHERE id=?', f['id'])


def _past_final(db: Database, finding_id: int) -> bool:
    return bool(db.one("SELECT 1 FROM jobs WHERE kind='recheck' AND ref_id=? AND stage='final' AND done=1", finding_id)) or \
        not db.one("SELECT 1 FROM jobs WHERE kind='recheck' AND ref_id=? AND done=0", finding_id)


def _resolve_contact(crm, p):
    contact = p.get('contact_id')
    if not contact and p.get('caller_phone') and hasattr(crm, 'contacts_by_phone'):
        found = crm.contacts_by_phone(p['caller_phone'])
        contact = found[0] if found else None
    return contact


def _finish(db, f, prev, state, reason, verdict, fields, stage, client, actor):
    db.insert('observations', agency_id=f['agency_id'], finding_id=f['id'], appointment_id=None, fields=dumps(fields),
              verdict=verdict, reason=reason, stage=stage, source='sandbox' if client['is_sandbox'] else 'highlevel', read_at=iso(now()))
    db.update('findings', {'id': f['id']}, state=state, reason=reason, updated_at=iso(now()))
    if state == 'verified':
        db.run("UPDATE jobs SET done=1 WHERE kind='recheck' AND ref_id=? AND done=0", f['id'])
    from . import cases
    cases.on_finding_state(db, f['id'], prev, state, actor=actor)
    return db.one('SELECT * FROM findings WHERE id=?', f['id'])


def check_callback(db: Database, f: dict, stage: str, actor: str = 'vaada') -> dict:
    """P0 #4: a promised callback must exist as a task and be completed before its due time."""
    from . import ledger
    client = db.one('SELECT * FROM clients WHERE id=?', f['client_id'])
    call = db.one('SELECT * FROM calls WHERE id=?', f['call_id'])
    p = f.j('promised', {})
    prev = f['state']
    try:
        crm = crm_for(db, client)
        contact = _resolve_contact(crm, p)
        linked = ledger.linked_contacts(crm, contact, p.get('caller_phone')) if contact else []
        tasks = [t for cid in linked for t in crm.tasks(cid)]
    except CRMError as e:
        return _finish(db, f, prev, 'unknown' if stage in ('final', 'manual') else 'pending', str(e), 'error', {}, stage, client, actor)
    from . import evidence
    start = parse_iso(call['started_at'])
    tasks = [t for t in tasks if not t.get('created_at') or parse_iso(t['created_at']) >= start - timedelta(minutes=1)]
    ids = evidence.tool_ids(loads(call['tool_calls'], []), r'task|callback') | set(p.get('recovery_ids') or [])
    task, link, ignored = evidence.match_callback(tasks, ids)
    calls_logged = []
    if task:
        try:
            since = parse_iso(task['created_at']) if task.get('created_at') else start
            for cid in linked:
                calls_logged += [m for m in crm.messages(cid) if 'call' in (m.get('channel') or '') and (m.get('direction') or '').startswith('out')
                                 and m.get('sent_at') and parse_iso(m['sent_at']) >= since - timedelta(minutes=1)]
        except CRMError:
            pass
    reached = any(evidence.REACHED.search(m.get('body') or '') for m in calls_logged)
    due = parse_iso(p['due_at']) if p.get('due_at') else None
    late_stage = stage in ('60m', 'final', 'manual')
    overdue = bool(due and now() > due)
    miss = 'missing' if (overdue or late_stage) else 'waiting'
    done = bool(task and task.get('completed'))
    via = 'linked by the ID the workflow returned' if link == 'id' else 'matched by its title; the workflow gave no task ID'
    stages = [evidence.stage('Task created', 'done' if task else miss, f"{task['id']}, “{task.get('title') or ''}” ({via})" if task else ''),
              evidence.stage('Call attempt logged', 'done' if calls_logged else ('unproven' if done else (miss if task else 'waiting')),
                             f'{len(calls_logged)} outbound call record(s)' if calls_logged else ('task ticked done, but no call is logged' if done else '')),
              evidence.stage('Customer reached', 'done' if reached else ('unknown' if calls_logged else ('unproven' if done else 'waiting')),
                             '' if reached or not calls_logged else 'the call record has no outcome'),
              evidence.stage('Request resolved', 'done' if done and calls_logged else ('unproven' if done else (miss if task else 'waiting')))]
    note = f" ({len(ignored)} other task(s) on this contact, e.g. “{ignored[0]['label']}”: {ignored[0]['why']})" if ignored else ''
    if not linked:
        state, reason, verdict = 'unknown', 'Could not identify the caller in the CRM, so the callback cannot be checked', 'identity'
    elif done and calls_logged:
        state, reason, verdict = 'verified', f"Callback task {task['id']} ({'linked by ID' if link == 'id' else 'matched by title'}) was completed and a call to the customer is logged", 'match'
    elif done:
        state, reason, verdict = 'unknown', f"Task {task['id']} is ticked done, but no call to the customer is logged. A checkbox isn’t proof of a callback.", 'unproven'
    elif task:
        if overdue:
            state, reason, verdict = 'contradicted', f"Callback task {task['id']} is still open past its {p.get('sla_hours', 4)}h deadline", 'overdue'
        else:
            state, reason, verdict = 'pending', 'Callback task exists; waiting for the call', 'open_task'
    else:
        state, reason, verdict = ('contradicted', 'The agent promised a callback but no task exists for it' + note, 'missing') if late_stage \
            else ('pending', 'No callback task yet; checking again later', 'missing')
    tasks = [task] if task else []
    fields = {'tasks': tasks[:10], 'linked_contacts': linked, 'due_at': p.get('due_at'), 'stages': stages, 'ignored': ignored[:5], 'link': link,
              'calls_logged': [{k: m.get(k) for k in ('id', 'sent_at', 'body')} for m in calls_logged[:5]]}
    if len(tasks) > 1:
        ledger.append(db, f, kind='task', key=ledger.intent_key(call['retell_call_id'], 'callback', {'c': contact}),
                      expected={'count': 1}, effects=tasks, attempts=0, tool_reported='n/a', verdict='duplicate', note='', stage=stage)
    return _finish(db, f, prev, state, reason, verdict, fields, stage, client, actor)


def check_contact(db: Database, f: dict, stage: str, actor: str = 'vaada') -> dict:
    """P0 #6 / P1 #8: a contact detail the caller asked to change must be changed in the CRM."""
    from .intents import same_value
    client = db.one('SELECT * FROM clients WHERE id=?', f['client_id'])
    p = f.j('promised', {})
    prev = f['state']
    try:
        crm = crm_for(db, client)
        contact = _resolve_contact(crm, p)
        rec = crm.contact(contact) if contact else None
    except CRMError as e:
        return _finish(db, f, prev, 'unknown' if stage in ('final', 'manual') else 'pending', str(e), 'error', {}, stage, client, actor)
    final = stage in ('final', 'manual')
    if not rec:
        state, reason, verdict = 'unknown', 'Could not find the caller in the CRM', 'identity'
    elif same_value(p['field'], p['value'], rec.get(p['field'])):
        state, reason, verdict = 'verified', f"CRM now shows the {p['field']} the caller asked for", 'match'
    elif final or prev == 'contradicted':
        state, reason, verdict = 'contradicted', f"Caller asked to change their {p['field']} to {p['value']}; the CRM still shows {rec.get(p['field']) or 'nothing'}", 'mismatch'
    else:
        state, reason, verdict = 'pending', f"CRM not updated yet; checking again later", 'mismatch'
    return _finish(db, f, prev, state, reason, verdict, {'contact': rec, 'requested': {p['field']: p['value']}}, stage, client, actor)
