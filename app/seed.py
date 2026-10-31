"""Sandbox agency for demos (`python -m app.seed`). Every record is synthetic
and labelled as sandbox in the UI; nothing here touches a real CRM."""
from __future__ import annotations

import json
from datetime import timedelta

from . import accounts, manifest, pipeline, trials, worker
from .db import Database, dumps, get_db, iso, now
from .integrations import retell
from .sim import SimAgent

DEMO_EMAIL = 'demo@vaada.dev'
DEMO_PASSWORD = 'vaada-demo-2026'

PROMPT = ("You are the virtual receptionist for {name}. Office hours are 8am to 5pm Monday to Friday. "
          "Book, reschedule and cancel appointments using the calendar tools. Patients receive a text confirmation after booking. "
          "If the caller asks for a person, transfer to the front desk.")


def _tools(*names):
    t = {'book': {'name': 'book_appointment', 'type': 'custom', 'parameters': {'properties': {'contactId': {}, 'calendarId': {}, 'startTime': {}}}},
         'avail': {'name': 'check_availability', 'type': 'check_availability_cal', 'parameters': {'properties': {'date': {}}}},
         'resched': {'name': 'reschedule_appointment', 'type': 'custom', 'parameters': {'properties': {'appointmentId': {}, 'startTime': {}}}},
         'cancel': {'name': 'cancel_appointment', 'type': 'custom', 'parameters': {'properties': {'appointmentId': {}}}},
         'transfer': {'name': 'transfer_call', 'type': 'transfer_call', 'parameters': {}},
         'end': {'name': 'end_call', 'type': 'end_call', 'parameters': {}}}
    return [t[n] for n in names]


def _snap(name, version, tools, sim, history=None, prompt=None):
    s = {'version': str(version), 'name': name, 'prompt': prompt or PROMPT.format(name=name), 'tools': tools, 'sim': sim}
    s['history'] = history or {}
    s['history'][str(version)] = {k: v for k, v in s.items() if k not in ('history', 'sim')}
    return s


def _approve(db, cid, policy, *, by, approver, role, at, agent_version):
    did = manifest.create_draft(db, cid, policy, by=by, source='auto')
    return manifest.approve(db, did, approver=approver, role=role, method='link' if role == 'client' else 'agency',
                            approved_agent_version=agent_version, effective_at=at, email=None)


def _call(db: Database, client_id: int, scenario: dict, started, seed: str, version=None, key=None):
    client = db.one('SELECT * FROM clients WHERE id=?', client_id)
    payload = SimAgent(db, client, seed, version).run(scenario, started)
    body = json.dumps(payload).encode()
    agency = db.one('SELECT * FROM agencies WHERE id=?', client['agency_id'])
    from .crypto import decrypt
    k = decrypt(agency['retell_api_key_enc'])
    assert retell.verify_signature(body, k, retell.sign(body, k))  # same path as the public webhook
    r = pipeline.ingest(db, client['agency_id'], payload, source='sandbox')
    return r.get('call_id')


def seed(db: Database | None = None, *, quiet=False) -> int:
    db = db or get_db()
    existing = db.one('SELECT agency_id FROM users WHERE email=?', DEMO_EMAIL)
    if existing:
        return existing['agency_id']
    from .crypto import encrypt, token
    aid = accounts.create_agency(db, 'Northstar Voice Co.', DEMO_EMAIL, 'Abhaya', DEMO_PASSWORD, sandbox=True)
    db.update('agencies', {'id': aid}, retell_api_key_enc=encrypt('sandbox_' + token()))
    t0 = now()
    # 1. Bright Smile Dental -------------------------------------------------
    s14 = _snap('Bright Smile Dental', 14, _tools('avail', 'book', 'resched', 'transfer', 'end'),
                {'ignores_correction': 0.25, 'leaks_discount': 0.3, 'promises_callback': 0.4, 'relative_date_bug': 0.2,
                 'double_book_reschedule': 0.2, 'texts': True},
                prompt=PROMPT.format(name='Bright Smile Dental') + ' Cleanings are $79. We can book root canals on weekdays.')
    bright = accounts.create_client(db, aid, name='Bright Smile Dental', location_label='Austin, TX', hl_location_id='loc_bright',
                                    retell_agent_id='agent_bright_smile', timezone='America/Chicago', sandbox=True,
                                    calendars=[{'id': 'cal_cleaning', 'name': 'Cleaning', 'slot_minutes': 60}], snapshot=s14)
    pol, _ = manifest.auto_draft(manifest.client_ctx(db, bright), s14)
    _approve(db, bright, {**pol, 'never_promise': ['discount', 'guarantee', 'price', 'refund']}, by='Abhaya', approver='Abhaya (Northstar)',
             role='agency', at=t0 - timedelta(days=12), agent_version='14')
    pol2 = {**pol, 'allowed_actions': ['book', 'reschedule'], 'never_promise': ['discount', 'guarantee', 'refund'],
            'deliverable_promises': ['callback', 'sms_confirmation'], 'callback_sla_hours': 2,
            'booking_rules': {'within_business_hours': True, 'min_notice_hours': 2, 'max_days_ahead': 60, 'excluded_services': ['root canal']},
            'escalations': [{'trigger': 'severe swelling', 'to': 'front_desk'}],
            'never_ask': ['card_number', 'password', 'otp', 'ssn'],
            'facts': {'prices': {'cleaning': 89, 'whitening': 299}, 'refund_window_days': 30, 'cancellation_notice_hours': 24,
                      'cancellation_fee': 25, 'accepts_insurance': True}}
    _approve(db, bright, pol2, by='Abhaya', approver='Dr. Priya Patel', role='client', at=t0 - timedelta(days=9), agent_version='14')
    # 2. Lone Star Plumbing: approved for v7, running v8 with a new cancel tool -------
    s7 = _snap('Lone Star Plumbing', 7, _tools('avail', 'book', 'end'), {'quotes_price': 0.5, 'no_transfer': 0.7, 'promises_callback': 0.6},
               prompt='You book plumbing visits for Lone Star Plumbing. Hours 7am to 6pm weekdays. We text a confirmation after booking.')
    lone = accounts.create_client(db, aid, name='Lone Star Plumbing', location_label='Dallas, TX', hl_location_id='loc_lonestar',
                                  retell_agent_id='agent_lone_star', timezone='America/Chicago', sandbox=True,
                                  calendars=[{'id': 'cal_service', 'name': 'Service visits', 'slot_minutes': 120}], snapshot=s7)
    lpol, _ = manifest.auto_draft(manifest.client_ctx(db, lone), s7)
    _approve(db, lone, {**lpol, 'handoff': {'on': 'caller_requests_person', 'to': 'dispatcher', 'hours': 'business'}},
             by='Abhaya', approver='Marcus Reed', role='client', at=t0 - timedelta(days=10), agent_version='7')
    s8 = _snap('Lone Star Plumbing', 8, _tools('avail', 'book', 'cancel', 'end'), s7['sim'], history=dict(s7['history']), prompt=s7['prompt'])
    db.update('clients', {'id': lone}, agent_snapshot=dumps(s8))
    # 3. Glow Med Spa: draft sent, nothing approved yet -------------------------
    sg = _snap('Glow Med Spa', 3, _tools('avail', 'book', 'end'), {'leaks_discount': 0.5})
    glow = accounts.create_client(db, aid, name='Glow Med Spa', location_label='Phoenix, AZ', hl_location_id='loc_glow',
                                  retell_agent_id='agent_glow', timezone='America/Phoenix', sandbox=True,
                                  calendars=[{'id': 'cal_consult', 'name': 'Consultations', 'slot_minutes': 30}], snapshot=sg)
    gpol, _ = manifest.auto_draft(manifest.client_ctx(db, glow), sg)
    gd = manifest.create_draft(db, glow, gpol, by='Abhaya', source='auto')
    manifest.send_for_approval(db, gd, to='owner@glowmedspa.example')

    # A call that started 40s after v2 took effect: IWCB judges it under v1 and v2 (boundary dual-judgment)
    c0 = db.one('SELECT * FROM clients WHERE id=?', bright)
    sb = json.loads(c0['agent_snapshot'])
    db.update('clients', {'id': bright}, agent_snapshot=dumps({**sb, 'sim': {**sb['sim'], 'asks_card': 1.0}}))
    _call(db, bright, {'kind': 'never_ask', 'rule': 'ssn'}, t0 - timedelta(days=9) + timedelta(seconds=40), 'boundary')
    db.update('clients', {'id': bright}, agent_snapshot=dumps(sb))
    # History: calls over the last 8 days ------------------------------------
    mix = [{'kind': 'book', 'variant': 'simple'}, {'kind': 'book', 'variant': 'correction'}, {'kind': 'never_promise', 'rule': 'discount'},
           {'kind': 'undeliverable', 'rule': 'callback'}, {'kind': 'book', 'variant': 'relative'}, {'kind': 'reschedule'},
           {'kind': 'handoff'}, {'kind': 'book', 'variant': 'simple'}, {'kind': 'never_promise', 'rule': 'price'}]
    n = 0
    for day in range(8, 0, -1):
        for j, sc in enumerate(mix[(day % 3):(day % 3) + 4]):
            st = (t0 - timedelta(days=day)).replace(hour=15 + j % 3, minute=10 * j)
            _call(db, bright, dict(sc), st, f'hist:{day}:{j}')
            if j < 2:
                _call(db, lone, dict({'kind': 'book', 'variant': 'simple'} if j == 0 else {'kind': 'handoff'}),
                      st + timedelta(minutes=7), f'lh:{day}:{j}', version='8' if day < 4 else '7')
            if j == 0:
                _call(db, glow, {'kind': 'never_promise', 'rule': 'discount'}, st + timedelta(minutes=3), f'gh:{day}')
            n += 1
    # Problems A and C, one call each -----------------------------------------
    def scene(over, sc, when, seed):
        c1 = db.one('SELECT agent_snapshot FROM clients WHERE id=?', bright)
        base = json.loads(c1['agent_snapshot'])
        db.update('clients', {'id': bright}, agent_snapshot=dumps({**base, 'sim': {**base['sim'], **over}}))
        _call(db, bright, sc, when, seed)
        db.update('clients', {'id': bright}, agent_snapshot=dumps(base))
    calm = {'ignores_correction': 0, 'relative_date_bug': 0, 'double_book_reschedule': 0}
    scene({**calm, 'wrong_fact': 1}, {'kind': 'fact', 'rule': 'prices.cleaning', 'value': 89}, t0 - timedelta(days=2, hours=3), 'A:price')
    scene({**calm, 'kb_fail': 1}, {'kind': 'fact', 'rule': 'refund_window_days', 'value': 30}, t0 - timedelta(days=1, hours=2), 'A:kb')
    scene({**calm, 'timeout_landed': 1}, {'kind': 'book', 'variant': 'simple', 'contact': 'ct_ana_p', 'caller_name': 'Ana Park'},
          t0 - timedelta(hours=30), 'C:timeout')
    scene({**calm, 'retry_dup': 1}, {'kind': 'book', 'variant': 'simple', 'contact': 'ct_leo_b', 'caller_name': 'Leo Brandt'},
          t0 - timedelta(hours=5), 'C:retry')
    scene({**calm, 'dup_contact': 1}, {'kind': 'book', 'variant': 'simple', 'contact': 'ct_sam_k', 'caller_name': 'Sam Kim'},
          t0 - timedelta(days=2, hours=1), 'C:dupcontact')
    scene({**calm, 'double_text': 1}, {'kind': 'book', 'variant': 'simple', 'contact': 'ct_ivy_t', 'caller_name': 'Ivy Tran'},
          t0 - timedelta(hours=6), 'C:doubletext')
    scene({**calm, 'no_text': 1}, {'kind': 'book', 'variant': 'simple', 'contact': 'ct_raj_m', 'caller_name': 'Raj Mehta'},
          t0 - timedelta(hours=26), 'B:notext')
    # The P0 list, one call each -----------------------------------------------
    p0 = [({'interrupt_bug': 1}, {'kind': 'book', 'variant': 'interrupt', 'contact': 'ct_tue_thu'}, timedelta(hours=7)),
          ({'books_busy': 1}, {'kind': 'book', 'variant': 'unavailable', 'contact': 'ct_busy_slot'}, timedelta(hours=9)),
          ({'tz_bug': 1}, {'kind': 'book', 'variant': 'timezone', 'contact': 'ct_ny_caller'}, timedelta(days=1, hours=4)),
          ({'no_recovery': 1}, {'kind': 'transfer_failure', 'contact': 'ct_xfer_fail'}, timedelta(days=3, hours=1)),
          ({'no_task': 1}, {'kind': 'callback', 'contact': 'ct_no_callback'}, timedelta(days=1, hours=6)),
          ({'ignores_rules': 1}, {'kind': 'rule_hours', 'contact': 'ct_saturday'}, timedelta(days=2, hours=5)),
          ({'ignores_rules': 1}, {'kind': 'rule_service', 'rule': 'root canal', 'contact': 'ct_rootcanal'}, timedelta(days=3, hours=3)),
          ({'no_transfer': 1}, {'kind': 'escalation', 'rule': 'severe swelling', 'to': 'front_desk', 'contact': 'ct_swelling'}, timedelta(days=4, hours=2)),
          ({'skip_update': 1}, {'kind': 'multi', 'contact': 'ct_multi'}, timedelta(days=1, hours=8))]
    for i, (over, sc, back) in enumerate(p0):
        when = t0 - back
        loc = when.astimezone(__import__('zoneinfo').ZoneInfo('America/Chicago'))
        while loc.weekday() >= 5 or not (9 <= loc.hour < 16):   # inside business hours so handoff and escalation rules apply
            when -= timedelta(hours=3)
            loc = when.astimezone(__import__('zoneinfo').ZoneInfo('America/Chicago'))
        scene({**calm, **over}, sc, when, f'P0:{i}')
    # The demo scene: "not 9, make it 10" -> calendar shows 9 -----------------
    client = db.one('SELECT * FROM clients WHERE id=?', bright)
    snap = json.loads(client['agent_snapshot'])
    snap['sim'] = {**snap['sim'], 'ignores_correction': 1.0}
    db.update('clients', {'id': bright}, agent_snapshot=dumps(snap))
    _call(db, bright, {'kind': 'book', 'variant': 'correction', 'contact': 'ct_maria_g', 'caller_name': 'Maria Gomez'},
          t0 - timedelta(minutes=75), 'scene:main')
    # A write that lands 5 minutes late (plays out live: pending -> verified)
    snap['sim'] = {**snap['sim'], 'ignores_correction': 0.0, 'late_write_min': 5}
    db.update('clients', {'id': bright}, agent_snapshot=dumps(snap))
    _call(db, bright, {'kind': 'book', 'variant': 'simple', 'contact': 'ct_jon_r', 'caller_name': 'Jon Reyes'}, t0 - timedelta(minutes=3), 'scene:late')
    snap['sim'] = {**snap['sim'], 'ignores_correction': 0.5, 'late_write_min': 0}  # trials will surface it
    db.update('clients', {'id': bright}, agent_snapshot=dumps(snap))
    # settle everything that is due now
    for _ in range(300):
        if worker.tick(db) == 0:
            break
    # one trial per approved client
    for cid in (bright, lone):
        tid = trials.create_trial(db, cid, runs=3, by='Abhaya')
        trials.run_sandbox_trial(db, tid)
        db.run("UPDATE jobs SET done=1 WHERE kind='trial' AND ref_id=?", tid)
    if not quiet:
        print(f'Seeded sandbox agency #{aid}: login {DEMO_EMAIL} / {DEMO_PASSWORD}')
    return aid


if __name__ == '__main__':
    seed()
