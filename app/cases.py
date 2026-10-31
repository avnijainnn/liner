"""Cases and the fix-and-verify loop.

Booking findings that are contradicted or unknown become cases. A case closes
as verified only when a fresh calendar read matches. Customer communication is
tracked separately: a fixed record whose caller was told the wrong thing stays
open until the operator marks the customer contacted. Every action is an
append-only case event; evidence is never edited.
"""
from __future__ import annotations

from datetime import timedelta

from .db import Database, dumps, iso, loads, now, parse_iso


def _priority(appt_at, state):
    if appt_at is None:
        return ('P2' if state == 'contradicted' else 'P3'), now() + timedelta(hours=24)
    left = appt_at - now()
    if left < timedelta(0):  # appointment already passed: customer follow-up, not a race
        return 'P3', now() + timedelta(hours=24)
    if left < timedelta(hours=24):
        return 'P1', max(now() + timedelta(minutes=30), min(appt_at - timedelta(hours=2), now() + timedelta(hours=4)))
    if left < timedelta(hours=72):
        return 'P2', min(appt_at - timedelta(hours=24), now() + timedelta(hours=24))
    return 'P3', now() + timedelta(hours=72)


def event(db: Database, case_id: int, actor: str, action: str, **detail):
    agency = db.val('SELECT agency_id FROM cases WHERE id=?', case_id)
    db.insert('case_events', agency_id=agency, case_id=case_id, actor=actor, action=action, detail=dumps(detail), at=iso(now()))


def ensure_case(db: Database, finding_id: int, actor='vaada') -> int:
    c = db.one('SELECT id FROM cases WHERE finding_id=?', finding_id)
    if c:
        return c['id']
    f = db.one('SELECT * FROM findings WHERE id=?', finding_id)
    pr, due = _priority(parse_iso(f['appointment_at']), f['state'])
    t = iso(now())
    cid = db.insert('cases', agency_id=f['agency_id'], client_id=f['client_id'], finding_id=finding_id, status='open',
                    priority=pr, due_at=iso(due), customer_contact='needed' if f['state'] == 'contradicted' else 'unknown',
                    created_at=t, updated_at=t)
    event(db, cid, actor, 'opened', state=f['state'], reason=f['reason'])
    return cid


def on_finding_state(db: Database, finding_id: int, prev: str, new: str, actor='vaada'):
    if prev == new and new != 'verified':
        c = db.one('SELECT id FROM cases WHERE finding_id=?', finding_id)
        if c:
            event(db, c['id'], actor, 'rechecked', state=new)
        return
    if new in ('contradicted', 'unknown'):
        cid = ensure_case(db, finding_id, actor)
        if new == 'contradicted':
            db.run("UPDATE cases SET customer_contact='needed', updated_at=? WHERE id=? AND customer_contact IN ('unknown','not_needed')", iso(now()), cid)
        event(db, cid, actor, 'state_changed', from_state=prev, to_state=new)
    elif new == 'verified':
        c = db.one('SELECT * FROM cases WHERE finding_id=?', finding_id)
        if c and c['status'] in ('open',):
            t = iso(now())
            status = 'closed_verified' if c['customer_contact'] in ('contacted', 'not_needed', 'unknown') else 'fixed_awaiting_contact'
            db.update('cases', {'id': c['id']}, status=status, fixed_at=t, closed_at=t if status == 'closed_verified' else None, updated_at=t)
            event(db, c['id'], actor, 'verified_by_fresh_read', from_state=prev)


class CaseError(ValueError):
    pass


def act(db: Database, case_id: int, action: str, actor: str, *, owner: str = '', note: str = '') -> dict:
    from . import verify
    c = db.one('SELECT * FROM cases WHERE id=?', case_id)
    if not c:
        raise CaseError('Case not found')
    t = iso(now())
    if action == 'assign':
        if not owner.strip():
            raise CaseError('Pick an owner')
        db.update('cases', {'id': case_id}, owner=owner.strip()[:80], updated_at=t)
        event(db, case_id, actor, 'assigned', owner=owner.strip())
    elif action == 'note':
        if not note.strip():
            raise CaseError('Note is empty')
        event(db, case_id, actor, 'note', note=note.strip()[:2000])
    elif action in ('recheck', 'verify'):
        if c['status'] not in ('open', 'fixed_awaiting_contact', 'retesting'):
            raise CaseError('Case is closed')
        verify_fix(db, case_id, actor)
    elif action == 'contacted':
        db.update('cases', {'id': case_id}, customer_contact='contacted', updated_at=t)
        event(db, case_id, actor, 'customer_contacted', note=note.strip()[:500])
        c2 = db.one('SELECT * FROM cases WHERE id=?', case_id)
        if c2['status'] == 'fixed_awaiting_contact':
            db.update('cases', {'id': case_id}, status='closed_verified', closed_at=t, updated_at=t)
            event(db, case_id, actor, 'closed', why='fixed and customer contacted')
    elif action == 'waive':
        if not note.strip():
            raise CaseError('Waiving needs a reason')
        db.update('cases', {'id': case_id}, status='waived', closed_at=t, updated_at=t)
        db.update('findings', {'id': c['finding_id']}, state='waived', dismissed_reason=note.strip()[:500], updated_at=t)
        event(db, case_id, actor, 'waived', reason=note.strip())
    elif action == 'dispute':
        if not note.strip():
            raise CaseError('Say why you disagree; the evidence stays on record')
        event(db, case_id, actor, 'disputed', reason=note.strip())
    else:
        raise CaseError('Unknown action')
    return db.one('SELECT * FROM cases WHERE id=?', case_id)


RECORD = ('booking', 'message_promised', 'callback_promised', 'contact_update')


def review_finding(db: Database, finding_id: int, decision: str, actor: str, reason: str = ''):
    """Confirm (opens a case that closes only when a fix is verified) or dismiss with a reason (a false positive;
    the evidence and reason stay on record and feed precision tracking)."""
    f = db.one('SELECT * FROM findings WHERE id=?', finding_id)
    if not f:
        raise CaseError('Finding not found')
    if f['type'] in RECORD:
        raise CaseError('Record findings close only on a fresh read of the record')
    if decision == 'dismiss' and not reason.strip():
        raise CaseError('Dismissing needs a reason')
    if decision not in ('confirm', 'dismiss', 'reopen'):
        raise CaseError('Unknown decision')
    state = {'confirm': 'confirmed', 'dismiss': 'dismissed', 'reopen': 'open'}[decision]
    db.update('findings', {'id': finding_id}, state=state, dismissed_reason=reason.strip()[:500] or None,
              reviewed_by=actor, reviewed_at=iso(now()), updated_at=iso(now()))
    db.insert('audit_log', agency_id=f['agency_id'], actor=actor, action=f'finding.{decision}', entity='finding',
              entity_id=str(finding_id), detail=dumps({'reason': reason}), at=iso(now()))
    c = db.one('SELECT * FROM cases WHERE finding_id=?', finding_id)
    if decision == 'confirm':
        cid = ensure_case(db, finding_id, actor)
        db.update('cases', {'id': cid}, owner=c['owner'] if c and c['owner'] else actor, customer_contact='not_needed',
                  verification=VERIFY_HOW.get(f['type'], 'retest'), updated_at=iso(now()))
        event(db, cid, actor, 'confirmed', note='Fix the cause, then press Verify fix. Acknowledging does not close it.')
    elif decision == 'dismiss' and c and c['status'] in ('open', 'retesting'):
        db.update('cases', {'id': c['id']}, status='dismissed', closed_at=iso(now()), updated_at=iso(now()))
        event(db, c['id'], actor, 'dismissed', reason=reason)


VERIFY_HOW = {'agent_drift': 'agent_version', 'config_drift': 'agent_config', 'duplicate_effect': 'fresh_read', 'retry_risk': 'fresh_read'}


def verify_fix(db: Database, case_id: int, actor: str) -> dict:
    """The only way a case closes as fixed: an independent check passes.
    record findings -> fresh read; duplicates -> re-read the commitment's effects; drift -> re-read the live agent;
    everything else -> re-run the trial scenarios compiled from the rule that was broken."""
    from . import verify, trials, pipeline, manifest, binding
    c = db.one('SELECT * FROM cases WHERE id=?', case_id)
    f = db.one('SELECT * FROM findings WHERE id=?', c['finding_id'])
    client = db.one('SELECT * FROM clients WHERE id=?', f['client_id'])
    t = iso(now())
    event(db, case_id, actor, 'verify_requested')
    if f['type'] in RECORD:
        verify.check(db, f['id'], 'manual', actor=actor)
        return db.one('SELECT * FROM cases WHERE id=?', case_id)
    ok, why = False, ''
    if f['type'] in ('duplicate_effect', 'retry_risk'):
        com = f.j('promised', {}).get('commitment_id')
        if com:
            verify.check(db, com, 'manual', actor=actor)
            last = db.one('SELECT verdict FROM ledger_entries WHERE finding_id=? ORDER BY id DESC LIMIT 1', com)
            ok = bool(last and last['verdict'] == 'once')
            why = 'Fresh read: exactly one effect remains' if ok else f"Fresh read still shows: {last['verdict'] if last else 'no read'}"
    elif f['type'] in ('agent_drift', 'config_drift'):
        mv = manifest.latest(db, client['id'])
        snap = loads(client['agent_snapshot'], {}) or {}
        running = str(snap.get('version') or f['agent_version'])
        body = mv.j('body') if mv else {}
        if f['type'] == 'agent_drift':
            ok = bool(mv) and str(body['agent']['approved_agent_version']) == running
            why = (f'Manifest v{mv["version"]} is now approved for the running agent v{running}' if ok
                   else f'Running agent v{running} is still not the approved v{body.get("agent", {}).get("approved_agent_version")}')
        else:
            issues = pipeline.config_issues(body, pipeline.snapshot_for(db, client, running) or snap)
            ok = not issues
            why = 'Live agent prompt now matches the manifest' if ok else 'Still out of sync: ' + '; '.join(i['text'] for i in issues[:2])
    else:
        mv = manifest.latest(db, client['id'])
        scen = trials.scenarios_for_finding(f, mv.j('body')) if mv else []
        if not scen:
            raise CaseError('No trial scenario covers this rule any more; dismiss it with a reason instead')
        tid = trials.create_trial(db, client['id'], runs=3, by=actor, only=scen)
        db.update('cases', {'id': case_id}, status='retesting', verification=f'trial:{tid}', updated_at=t)
        event(db, case_id, actor, 'retest_started', trial=tid, scenarios=scen)
        if client['is_sandbox']:
            trials.run_sandbox_trial(db, tid)
            db.run("UPDATE jobs SET done=1 WHERE kind='trial' AND ref_id=?", tid)
        return db.one('SELECT * FROM cases WHERE id=?', case_id)
    _close_or_keep(db, c, f, ok, why, actor)
    return db.one('SELECT * FROM cases WHERE id=?', case_id)


def _close_or_keep(db, c, f, ok, why, actor):
    t = iso(now())
    if ok:
        db.update('cases', {'id': c['id']}, status='closed_verified', fixed_at=t, closed_at=t, updated_at=t)
        db.update('findings', {'id': f['id']}, state='resolved', updated_at=t)
        event(db, c['id'], actor, 'verified_fixed', why=why)
    else:
        db.update('cases', {'id': c['id']}, status='open', updated_at=t)
        event(db, c['id'], actor, 'verify_failed', why=why)


def resolve_retests(db: Database, trial_id: int):
    """Called when a trial finishes: close the cases whose retest it was, only if every run passed."""
    tr = db.one('SELECT * FROM trials WHERE id=?', trial_id)
    s = loads(tr['summary'], {})
    for c in db.all("SELECT * FROM cases WHERE verification=? AND status='retesting'", f'trial:{trial_id}'):
        f = db.one('SELECT * FROM findings WHERE id=?', c['finding_id'])
        st = s.get('status', {})
        ok = bool(st) and all(v == 'pass' for v in st.values())
        why = (f"Retest trial #{trial_id}: {s.get('passed')}/{s.get('scenarios')} scenarios passed every run" if ok
               else f"Retest trial #{trial_id} still fails: {', '.join(k for k, v in st.items() if v != 'pass')}")
        _close_or_keep(db, c, f, ok, why, 'vaada')
