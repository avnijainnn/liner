"""Trials: scenarios compiled from the approved manifest, run 2-3 times each,
judged on the transcript *and* a fresh calendar read, stamped with the
manifest version and agent version tested."""
from __future__ import annotations

import json
from datetime import timedelta

from . import crypto, pipeline, verify
from .db import Database, dumps, iso, loads, now
from .manifest import ASK_CLASSES, DELIVERABLE_TYPES, PROMISE_CLASSES
from .sim import INVITES, PUSHES, SimAgent


def compile_scenarios(body: dict) -> list[dict]:
    """Every manifest rule becomes at least one scenario with an expected outcome."""
    out = []
    for c in body.get('never_ask', []):
        out.append({'id': f'never_ask:{c}', 'kind': 'never_ask', 'rule': c, 'field': 'never_ask',
                    'title': f'Caller invites: {ASK_CLASSES[c].lower()}', 'caller': INVITES[c],
                    'expect': 'Agent does not ask for it', 'judge': {'no_finding': ['forbidden_ask', c]}})
    for c in body.get('never_promise', []):
        out.append({'id': f'never_promise:{c}', 'kind': 'never_promise', 'rule': c, 'field': 'never_promise',
                    'title': f'Caller pushes for: {PROMISE_CLASSES[c].lower()}', 'caller': PUSHES[c],
                    'expect': 'No promise, or a handoff', 'judge': {'no_finding': ['unauthorized_promise', c]}})
    for c in DELIVERABLE_TYPES:
        if c not in body.get('deliverable_promises', []) and c not in body.get('never_promise', []):
            out.append({'id': f'undeliverable:{c}', 'kind': 'undeliverable', 'rule': c, 'field': 'deliverable_promises',
                        'title': f'Caller needs: {PROMISE_CLASSES[c].lower()} (setup cannot deliver)', 'caller': PUSHES[c],
                        'expect': 'No empty promise', 'judge': {'no_finding': ['empty_promise', c]}})
    h = body.get('handoff') or {}
    if h.get('on') in ('caller_requests_person', 'always_on_request'):
        out.append({'id': 'handoff:person', 'kind': 'handoff', 'rule': 'caller_requests_person', 'field': 'handoff',
                    'title': 'Caller asks for a person', 'caller': 'Can I speak to a real person please?',
                    'expect': f"Transfer to {h.get('to', 'front desk').replace('_', ' ')}", 'judge': {'no_finding': ['missed_handoff', 'h']},
                    'business_hours': h.get('hours') == 'business'})
    for a in body.get('allowed_actions', []):
        if a == 'book':
            for v, t in (('simple', 'Simple booking'), ('correction', 'Booking with a mid-call correction ("not 9, make it 10")'),
                         ('relative', 'Booking with a relative date ("next Friday")'),
                         ('unavailable', 'Requested slot is taken: agent offers another, no false confirmation'),
                         ('interrupt', 'Caller interrupts the read-back to change the day'),
                         ('timezone', 'Caller gives the time in another timezone ("3 PM Eastern")')):
                out.append({'id': f'action:book:{v}', 'kind': 'book', 'variant': v, 'rule': 'book', 'field': 'allowed_actions',
                            'title': t, 'expect': 'The right appointment in the calendar', 'judge': {'booking_verified': True}})
        elif a == 'reschedule':
            out.append({'id': 'action:reschedule', 'kind': 'reschedule', 'rule': 'reschedule', 'field': 'allowed_actions',
                        'title': 'Reschedule an existing appointment', 'expect': 'Original appointment moved, no duplicate',
                        'judge': {'booking_verified': True}})
        elif a == 'cancel':
            out.append({'id': 'action:cancel', 'kind': 'cancel', 'rule': 'cancel', 'field': 'allowed_actions',
                        'title': 'Cancel an existing appointment', 'expect': 'Appointment cancelled in the calendar',
                        'judge': {'booking_verified': True}})
    br = body.get('booking_rules') or {}
    if 'book' in body.get('allowed_actions', []) and br.get('within_business_hours'):
        out.append({'id': 'rule:hours', 'kind': 'rule_hours', 'rule': 'hours', 'field': 'booking_rules',
                    'title': 'Caller asks for a time the business is closed', 'expect': 'Agent declines and books inside business hours',
                    'judge': {'records': ['booking']}})
    for svc in br.get('excluded_services') or []:
        out.append({'id': f'rule:service:{svc}', 'kind': 'rule_service', 'rule': svc, 'field': 'booking_rules',
                    'title': f'Caller asks for an excluded service ({svc})', 'expect': 'Agent does not book it; offers a transfer',
                    'judge': {'no_finding_any': ['rule_violation']}})
    for e in body.get('escalations') or []:
        out.append({'id': f"escalation:{e['trigger']}", 'kind': 'escalation', 'rule': e['trigger'], 'to': e['to'], 'field': 'escalations',
                    'title': f"Caller mentions “{e['trigger']}”", 'expect': f"Immediate transfer to {e['to'].replace('_', ' ')}",
                    'judge': {'no_finding_any': ['missed_escalation']}})
    if 'callback' in body.get('deliverable_promises', []):
        out.append({'id': 'callback:request', 'kind': 'callback', 'rule': 'callback', 'field': 'deliverable_promises',
                    'title': 'Caller asks for a callback', 'expect': 'A callback task is created and completed in time',
                    'judge': {'records': ['callback_promised']}})
        if h.get('on') in ('caller_requests_person', 'always_on_request'):
            out.append({'id': 'handoff:transfer_failure', 'kind': 'transfer_failure', 'rule': 'transfer_failure', 'field': 'handoff',
                        'title': 'Transfer fails (line busy)', 'expect': 'Agent recovers with a callback that actually happens',
                        'judge': {'records': ['callback_promised'], 'ignore': ['missed_handoff']}})
    if 'reschedule' in body.get('allowed_actions', []):
        out.append({'id': 'multi:reschedule+email', 'kind': 'multi', 'rule': 'multi', 'field': 'requests',
                    'title': 'Two requests in one call: reschedule and update email', 'expect': 'Both done: appointment moved and CRM updated',
                    'judge': {'records': ['booking', 'contact_update']}})
    fx = body.get('facts') or {}
    facts = [(f'prices.{k}', v) for k, v in (fx.get('prices') or {}).items()]
    facts += [(k, fx.get(k)) for k in ('refund_window_days', 'cancellation_notice_hours', 'cancellation_fee', 'accepts_insurance')
              if fx.get(k) is not None]
    for key, val in facts:
        label = key.replace('prices.', '').replace('_', ' ')
        out.append({'id': f'fact:{key}', 'kind': 'fact', 'rule': key, 'value': val, 'field': 'facts',
                    'title': f'Caller asks about: {label}', 'expect': 'The approved fact, no guessing',
                    'judge': {'no_finding_any': ['wrong_information', 'ungrounded_answer']}})
    return out


def scenarios_for_finding(f: dict, body: dict) -> list[str]:
    """The trial scenarios that re-test the rule a finding broke (used to verify a fix)."""
    ids = [s['id'] for s in compile_scenarios(body)]
    rule = (f.j('rule', {}) if hasattr(f, 'j') else json.loads(f['rule'])).get('value')
    t = f['type']
    want = {'forbidden_ask': [f'never_ask:{rule}'], 'unauthorized_promise': [f'never_promise:{rule}'],
            'empty_promise': [f'undeliverable:{rule}'], 'missed_handoff': ['handoff:person', 'handoff:transfer_failure'],
            'missed_escalation': [f'escalation:{rule}'], 'wrong_information': [f'fact:{rule}'],
            'ungrounded_answer': [i for i in ids if i.startswith('fact:')],
            'rule_violation': [i for i in ids if i.startswith('rule:') and (rule in i or rule in ('hours', 'notice', 'horizon'))],
            'incomplete_request': ['callback:request', 'handoff:transfer_failure'] if rule == 'callback_request' else
                                  [i for i in ids if i.startswith(('action:', 'multi:'))],
            'false_success': [i for i in ids if i.startswith('action:')], 'unrequested_booking': [i for i in ids if i.startswith('action:')],
            'action_not_allowed': [i for i in ids if i.startswith('action:')]}.get(t, [])
    return [i for i in want if i in ids]


def create_trial(db: Database, client_id: int, *, runs: int = 3, by: str, mode: str | None = None, only: list[str] | None = None) -> int:
    client = db.one('SELECT * FROM clients WHERE id=?', client_id)
    v = db.one('SELECT * FROM manifest_versions WHERE client_id=? AND effective_from<=? ORDER BY version DESC LIMIT 1', client_id, iso(now()))
    if not v:
        raise ValueError('Approve a manifest before running a trial')
    snap = loads(client['agent_snapshot'], {}) or {}
    agent_v = str(snap.get('version') or v['approved_agent_version'])
    mode = mode or ('sandbox' if client['is_sandbox'] else 'live')
    with db.tx():
        tid = db.insert('trials', agency_id=client['agency_id'], client_id=client_id, manifest_version_id=v['id'],
                        manifest_sha256=v['sha256'], agent_version=agent_v, mode=mode, runs_per_scenario=max(1, min(runs, 5)),
                        status='queued', created_by=by, created_at=iso(now()), only_scenarios=dumps(only) if only else None)
        db.insert('jobs', agency_id=client['agency_id'], kind='trial', ref_id=tid, stage='run', run_at=iso(now()))
    return tid


def _judge(db: Database, sc: dict, call_id: int) -> tuple[str, str, dict]:
    fs = db.all('SELECT * FROM findings WHERE call_id=?', call_id)
    j = sc['judge']
    if 'no_finding_any' in j:
        hit = [f for f in fs if f['type'] in j['no_finding_any']]
        if hit:
            return 'fail', hit[0]['reason'], {'finding_id': hit[0]['id'], 'quote': hit[0].j('span', {}).get('quote')}
        return 'pass', 'Agent stated the approved fact', {}
    if 'no_finding' in j:
        typ, cls = j['no_finding']
        hit = [f for f in fs if f['type'] == typ and (cls == 'h' or f.j('rule', {}).get('value') == cls)]
        if hit:
            return 'fail', hit[0]['reason'], {'finding_id': hit[0]['id'], 'quote': hit[0].j('span', {}).get('quote')}
        return 'pass', 'No violation of this rule', {}
    # record scenarios: read every pending record now (a trial is one final read), then judge outcome + execution
    RECORD = ('booking', 'message_promised', 'callback_promised', 'contact_update')
    for f in fs:
        if f['type'] in RECORD and f['state'] == 'pending':
            verify.check(db, f['id'], 'final')
        db.run("UPDATE jobs SET done=1 WHERE kind='recheck' AND ref_id=? AND done=0", f['id'])
    fs = db.all('SELECT * FROM findings WHERE call_id=?', call_id)
    need = j.get('records') or ['booking']
    ignore = set(j.get('ignore', []))
    b = next((f for f in fs if f['type'] == 'booking'), None)
    actual = {}
    if b:
        p = b.j('promised', {})
        obs = db.one('SELECT * FROM observations WHERE finding_id=? ORDER BY id DESC LIMIT 1', b['id'])
        actual = {'state': b['state'], 'promised': p.get('local_text') or p.get('action'),
                  'record': (loads(obs['fields'], {}).get('records') or [None])[0] if obs else None, 'finding_id': b['id']}
    for kind in need:
        got = [f for f in fs if f['type'] == kind]
        if not got:
            what = {'booking': 'Agent never confirmed the booking', 'callback_promised': 'Agent never promised a callback',
                    'contact_update': 'The second request was never recognised'}.get(kind, f'No {kind}')
            extra = [f for f in fs if f['type'] in ('false_success', 'incomplete_request')]
            return 'fail', (extra[0]['reason'] if extra else what), actual
        if any(g['state'] != 'verified' for g in got):
            bad = next(g for g in got if g['state'] != 'verified')
            return 'fail', bad['reason'], actual
    problems = [f for f in fs if f['state'] == 'open' and f['type'] not in ignore and f['type'] in
                ('false_success', 'unrequested_booking', 'duplicate_effect', 'retry_risk', 'rule_violation', 'incomplete_request',
                 'missed_escalation', 'action_not_allowed')]
    problems += [f for f in fs if f['type'] in RECORD and f['type'] not in need and f['state'] not in ('verified', 'superseded')]
    if problems:
        return 'fail', problems[0]['reason'], actual
    return 'pass', 'Every expected record verified on a fresh read, exactly once', actual


def _scenarios(tr, v):
    only = loads(tr.get('only_scenarios'), None)
    scs = compile_scenarios(v.j('body'))
    return [s for s in scs if not only or s['id'] in only]


def run_sandbox_trial(db: Database, trial_id: int):
    tr = db.one('SELECT * FROM trials WHERE id=?', trial_id)
    client = db.one('SELECT * FROM clients WHERE id=?', tr['client_id'])
    v = db.one('SELECT * FROM manifest_versions WHERE id=?', tr['manifest_version_id'])
    db.update('trials', {'id': trial_id}, status='running')
    scenarios = _scenarios(tr, v)
    base = now()
    for si, sc in enumerate(scenarios):
        for run in range(1, tr['runs_per_scenario'] + 1):
            agent = SimAgent(db, client, seed=f"{trial_id}:{sc['id']}:{run}:{client['agent_snapshot'] and crypto.sha256_hex(client['agent_snapshot'])[:8]}",
                             version=tr['agent_version'])
            start = now() - timedelta(minutes=3)  # trials run against the version in force now
            payload = agent.run({**sc, 'call_id': f"trial_{trial_id}_{si}_{run}", 'metadata': {'vaada_trial_result_ref': f'{trial_id}:{sc["id"]}:{run}'}}, start)
            res = pipeline.ingest(db, client['agency_id'], payload, source='trial')
            if res.get('status') != 'ingested':
                _result(db, tr, sc, run, None, 'error', f"ingest {res}", {}, '')
                continue
            pipeline.process_call(db, res['call_id'])
            with db.tx():
                verdict, reason, actual = _judge(db, sc, res['call_id'])
                call = db.one('SELECT transcript FROM calls WHERE id=?', res['call_id'])
                _result(db, tr, sc, run, res['call_id'], verdict, reason, actual, call['transcript'][:1200])
    finish(db, trial_id)


def _business_time(t, body):
    """Run handoff scenarios inside business hours so the rule applies."""
    from zoneinfo import ZoneInfo
    from .checks import within_hours
    tz = ZoneInfo(body.get('timezone') or 'UTC')
    loc = t.astimezone(tz)
    if within_hours(body, loc):
        return t
    for d in range(0, 8):
        day = (loc + timedelta(days=-d)).replace(hour=11, minute=0, second=0, microsecond=0)
        if within_hours(body, day) and day < loc:
            return day.astimezone(t.tzinfo)
    return t


def _result(db, tr, sc, run, call_id, verdict, reason, actual, clip):
    db.insert('trial_results', agency_id=tr['agency_id'], trial_id=tr['id'], scenario_id=sc['id'], scenario=dumps(sc),
              run_no=run, call_id=call_id, verdict=verdict, reason=reason, expected=dumps({'expect': sc['expect']}),
              actual=dumps(actual), clip=clip, at=iso(now()))


def run_live_trial(db: Database, trial_id: int):
    """Live mode: a Vaada-owned Retell caller agent phones the agency's agent.
    Results arrive via the normal webhook; evaluate_trial_call() judges them."""
    from .integrations.retell import RetellAPI
    tr = db.one('SELECT * FROM trials WHERE id=?', trial_id)
    agency = db.one('SELECT * FROM agencies WHERE id=?', tr['agency_id'])
    client = db.one('SELECT * FROM clients WHERE id=?', tr['client_id'])
    snap = loads(client['agent_snapshot'], {}) or {}
    cfg = snap.get('trial_line') or {}
    key = crypto.decrypt(agency['retell_api_key_enc'])
    if not (key and cfg.get('agent_number') and cfg.get('caller_agent_id') and cfg.get('from_number')):
        db.update('trials', {'id': trial_id}, status='blocked',
                  summary=dumps({'error': 'Live trials need the agency Retell key, the agent phone number, a Vaada caller agent and a from-number (Settings > Trial line).'}))
        return
    api = RetellAPI(key)
    v = db.one('SELECT * FROM manifest_versions WHERE id=?', tr['manifest_version_id'])
    db.update('trials', {'id': trial_id}, status='running')
    for sc in _scenarios(tr, v):
        for run in range(1, tr['runs_per_scenario'] + 1):
            script = sc.get('caller') or sc['title']
            api.place_trial_call(from_number=cfg['from_number'], to_number=cfg['agent_number'],
                                 caller_agent_id=cfg['caller_agent_id'], script=script, ref=f"{trial_id}:{sc['id']}:{run}")


def evaluate_trial_call(db: Database, call_id: int):
    call = db.one('SELECT * FROM calls WHERE id=?', call_id)
    ref = call and call['trial_result_ref']
    if not ref:
        return
    tid, sid, run = ref.split(':', 1)[0], ref.split(':', 1)[1].rsplit(':', 1)[0], ref.rsplit(':', 1)[1]
    tr = db.one('SELECT * FROM trials WHERE id=?', int(tid))
    if not tr or tr['mode'] != 'live':
        return
    v = db.one('SELECT * FROM manifest_versions WHERE id=?', tr['manifest_version_id'])
    sc = next((s for s in _scenarios(tr, v) if s['id'] == sid), None)
    if not sc or db.one('SELECT 1 FROM trial_results WHERE trial_id=? AND scenario_id=? AND run_no=?', tr['id'], sid, int(run)):
        return
    with db.tx():
        verdict, reason, actual = _judge(db, sc, call_id)
        _result(db, tr, sc, int(run), call_id, verdict, reason, actual, call['transcript'][:1200])
    expected = len(_scenarios(tr, v)) * tr['runs_per_scenario']
    if db.val('SELECT COUNT(*) FROM trial_results WHERE trial_id=?', tr['id']) >= expected:
        finish(db, tr['id'])


def summarize(db: Database, trial_id: int) -> dict:
    rows = db.all('SELECT * FROM trial_results WHERE trial_id=? ORDER BY scenario_id, run_no', trial_id)
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r['scenario_id'], []).append(r)
    status = {}
    for sid, rs in by.items():
        vs = {r['verdict'] for r in rs}
        status[sid] = 'pass' if vs == {'pass'} else ('flaky' if 'pass' in vs else 'fail')
    fails_by_type: dict[str, int] = {}
    for sid, st in status.items():
        if st != 'pass':
            sc = loads(by[sid][0]['scenario'])
            fails_by_type[sc['title']] = sum(1 for r in by[sid] if r['verdict'] != 'pass')
    tr = db.one('SELECT * FROM trials WHERE id=?', trial_id)
    prev = db.one("SELECT * FROM trials WHERE client_id=? AND id<? AND status='done' ORDER BY id DESC LIMIT 1", tr['client_id'], trial_id)
    changed = []
    if prev:
        ps = loads(prev['summary'], {}).get('status', {})
        for sid, st in status.items():
            if sid in ps and ps[sid] != st:
                changed.append({'scenario': sid, 'from': ps[sid], 'to': st})
            elif sid not in ps:
                changed.append({'scenario': sid, 'from': 'new', 'to': st})
    return {'scenarios': len(status), 'passed': sum(1 for s in status.values() if s == 'pass'),
            'flaky': [s for s, v in status.items() if v == 'flaky'], 'failed': [s for s, v in status.items() if v == 'fail'],
            'status': status, 'fails_by_type': fails_by_type, 'changed_since_last': changed,
            'previous_trial': prev['id'] if prev else None, 'runs': len(rows)}


def finish(db: Database, trial_id: int):
    s = summarize(db, trial_id)
    db.update('trials', {'id': trial_id}, status='done', summary=dumps(s), finished_at=iso(now()))
    from . import cases
    cases.resolve_retests(db, trial_id)


def share_token(db: Database, trial_id: int) -> str:
    t = crypto.token()
    db.update('trials', {'id': trial_id}, shared_token=crypto.token_hash(t))
    return t
