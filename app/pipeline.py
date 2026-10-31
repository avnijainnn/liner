"""Monitor: what happens after every call (spec, Mode 2 steps 1-7).

Trial calls and real calls take the same path. Each call is bound to the
manifest and agent version in force at its start before any check runs.
"""
from __future__ import annotations

import re

from . import binding, checks, config, crypto
from . import facts as facts_mod, intents
from . import manifest as manifest_mod
from datetime import timedelta
from .db import Database, dumps, iso, loads, now, parse_iso
from .extract import extract_commitment
from .integrations import retell
from .labeler import label
from .timeparse import to_utc
from .verify import schedule_reads

CARD = re.compile(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)')
OTP = re.compile(r'(?i)\b((?:otp|one[- ]time (?:code|password)|verification code|passcode|code is)\s*(?:is|:)?\s*)(\d[\d -]{3,9}\d)\b')
SSN = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')


def redact(s: str) -> str:
    """Card numbers, one-time codes and SSNs are removed before storage."""
    s = CARD.sub('[card number removed]', s)
    s = OTP.sub(lambda m: m.group(1) + '[code removed]', s)
    return SSN.sub('[ssn removed]', s)


def redact_obj(v):
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, list):
        return [redact_obj(x) for x in v]
    if isinstance(v, dict):
        return {k: redact_obj(x) for k, x in v.items()}
    return v


def config_issues(body: dict, snapshot: dict) -> list[dict]:
    """Instructions in the live agent's prompt that contradict the approved manifest."""
    prompt = (snapshot or {}).get('prompt') or ''
    if not prompt:
        return []
    from .labeler import rules_labels
    pseudo = [{'i': 0, 'role': 'agent', 'text': prompt}]
    out = []
    for c in facts_mod.claims_in_text(prompt, body.get('facts') or {}):
        fact = facts_mod.fact_value(c['key'], body)
        if fact is not None and not facts_mod.same(c['said'], fact):
            out.append({'kind': 'stale_fact', 'quote': c['quote'],
                        'text': f"prompt says {c['label']} is {facts_mod.fmt(c['key'], c['said'])}, manifest says {facts_mod.fmt(c['key'], fact)}"})
    for l in rules_labels(pseudo):
        if l.kind == 'promise' and l.cls in body.get('never_promise', []):
            out.append({'kind': 'forbidden_instruction', 'quote': l.quote, 'text': f"prompt still tells the agent to offer {l.cls.replace('_', ' ')}"})
    caps = manifest_mod.capabilities(snapshot)
    for svc in (body.get('booking_rules') or {}).get('excluded_services') or []:
        if re.search(rf'\b(?:book|schedule)\b[^.]{{0,40}}{re.escape(svc)}', prompt, re.I):
            out.append({'kind': 'excluded_service', 'quote': svc, 'text': f'prompt still says the agent can book {svc}'})
    if (body.get('handoff') or {}).get('on') not in (None, 'never') and 'handoff' not in caps and snapshot.get('tools'):
        out.append({'kind': 'missing_tool', 'quote': '', 'text': 'manifest requires transfers but the live agent has no transfer tool'})
    return out


def agent_api(db: Database, client: dict):
    """Read-only agent API for this client: Retell agent IDs start with 'agent_'; anything else with a Vapi key is a Vapi assistant."""
    from .integrations import vapi
    a = db.one('SELECT retell_api_key_enc, vapi_api_key_enc FROM agencies WHERE id=?', client['agency_id'])
    rk = crypto.decrypt(a['retell_api_key_enc']) if a and a['retell_api_key_enc'] else None
    vk = crypto.decrypt(a['vapi_api_key_enc']) if a and a['vapi_api_key_enc'] else None
    aid = str(client.get('retell_agent_id') or '')
    if vk and (not rk or not aid.startswith('agent_')):
        return vapi.VapiAPI(vk), 'Vapi'
    return retell.RetellAPI(rk or ''), 'Retell'


def snapshot_for(db: Database, client: dict, version: str):
    snap = client.get('agent_snapshot')
    if isinstance(snap, str):
        snap = loads(snap, None)
    if not snap:
        return None
    hist = snap.get('history') or {}
    if str(version) in hist:
        return hist[str(version)]
    if str(snap.get('version')) == str(version):
        return snap
    if not client.get('is_sandbox'):
        try:  # read-only fetch of the version that actually ran
            api, _ = agent_api(db, client)
            return api.agent_snapshot(client['retell_agent_id'], version)
        except Exception:
            return None
    return None


# ----------------------------------------------------------------- 1. ingest
def ingest(db: Database, agency_id: int, payload: dict, *, source='retell') -> dict:
    ev = payload.get('event')
    call = payload.get('call') or {}
    cid = call.get('call_id')
    if ev not in ('call_ended', 'call_analyzed'):
        db.insert('webhook_events', agency_id=agency_id, event=ev, call_id=cid, signature_ok=1, outcome='ignored_event', received_at=iso(now()))
        return {'status': 'ignored', 'event': ev}
    if not cid or not call.get('agent_id'):
        db.insert('webhook_events', agency_id=agency_id, event=ev, call_id=cid, signature_ok=1, outcome='missing_ids', received_at=iso(now()))
        return {'status': 'rejected', 'reason': 'missing call_id or agent_id'}
    client = db.one('SELECT * FROM clients WHERE agency_id=? AND retell_agent_id=?', agency_id, call['agent_id'])
    if not client:
        db.insert('webhook_events', agency_id=agency_id, event=ev, call_id=cid, signature_ok=1, outcome='unknown_agent', received_at=iso(now()))
        return {'status': 'ignored', 'reason': 'agent not registered to a client'}
    with db.tx():
        if db.one('SELECT id FROM calls WHERE retell_call_id=?', cid):
            db.insert('webhook_events', agency_id=agency_id, event=ev, call_id=cid, signature_ok=1, outcome='duplicate', received_at=iso(now()))
            return {'status': 'duplicate'}
        n = retell.normalise(call)
        start = end = None
        try:
            start = iso(to_utc(call['start_timestamp'], client['timezone'])) if call.get('start_timestamp') else None
            end = iso(to_utc(call['end_timestamp'], client['timezone'])) if call.get('end_timestamp') else None
        except (ValueError, TypeError):
            pass
        meta = {'transferred': n['transferred'], 'disconnection_reason': n['disconnection_reason'],
                'metadata': n['metadata'], 'event': ev, 'transfer_failed': n.get('transfer_failed', False), 'from_number': n.get('from_number'), 'to_number': n.get('to_number'),
                'direction': call.get('direction')}
        call_id = db.insert('calls', agency_id=agency_id, client_id=client['id'], retell_call_id=str(cid),
                            agent_id=call.get('agent_id'), agent_version=str(call.get('agent_version', 'unknown')),
                            started_at=start, ended_at=end or iso(now()), transcript=redact(n['transcript']),
                            turns=dumps(redact_obj(n['turns'])), tool_calls=dumps(redact_obj(n['tool_calls'])),
                            meta=dumps(meta), source=source, trial_result_ref=(n['metadata'] or {}).get('vaada_trial_result_ref'),
                            received_at=iso(now()))
        db.insert('call_state', call_id=call_id, agency_id=agency_id, status='ingested')
        db.insert('jobs', agency_id=agency_id, kind='process', ref_id=call_id, stage='process', run_at=iso(now()))
        db.insert('webhook_events', agency_id=agency_id, event=ev, call_id=cid, signature_ok=1, outcome='ingested', received_at=iso(now()))
    return {'status': 'ingested', 'call_id': call_id}


# ------------------------------------------------------------- 2-7. process
def _stamp(b_primary, envelope_key, call, agent_changed):
    return dict(manifest_version=b_primary['version'] if b_primary else None,
                manifest_sha256=b_primary['sha256'] if b_primary else None,
                signature_key_id=envelope_key, agent_version=str(call['agent_version']),
                agent_changed=1 if agent_changed else 0)


def _add_finding(db, call, client, ftype, state, reason, *, severity, rule=None, span=None, promised=None,
                 stamp=None, dedupe=None, appointment_at=None, boundary_note=None):
    if dedupe and db.one('SELECT id FROM findings WHERE dedupe_key=?', dedupe):
        return None
    t = iso(now())
    return db.insert('findings', agency_id=client['agency_id'], client_id=client['id'], call_id=call['id'], type=ftype,
                     severity=severity, state=state, reason=reason, rule=dumps(rule or {}), span=dumps(span or {}),
                     promised=dumps(promised or {}), dedupe_key=dedupe, appointment_at=appointment_at,
                     boundary_note=boundary_note, created_at=t, updated_at=t, **(stamp or {}))


def process_call(db: Database, call_id: int) -> dict:
    from . import cases
    with db.tx():
        call = db.one('SELECT * FROM calls WHERE id=?', call_id)
        st = db.one('SELECT * FROM call_state WHERE call_id=?', call_id)
        if not call or st['status'] != 'ingested':
            return {'status': 'skipped'}
        client = db.one('SELECT * FROM clients WHERE id=?', call['client_id'])
        running = snapshot_for(db, client, call['agent_version'])
        call = dict(call, agent_fingerprint=None)
        b, receipt, rid = binding.bind_call(db, call, config.BINDING_EPSILON_S, running)
        turns, tools, meta = loads(call['turns'], []), loads(call['tool_calls'], []), loads(call['meta'], {})
        state = dict(status='processed', binding_receipt_id=rid, binding_status=b.status, drift_class=receipt['drift'],
                     manifest_version_id=b.primary['id'] if b.primary else None,
                     manifest_sha256=b.primary['sha256'] if b.primary else None, processed_at=iso(now()))
        if not b.primary:
            db.update('call_state', {'call_id': call_id}, coverage='unknown_manifest', **state)
            return {'status': 'unbound', 'binding': b.status}
        body = b.primary.j('body')
        env = db.one('SELECT key_id FROM signature_envelopes WHERE manifest_version_id=?', b.primary['id'])
        agent_changed = receipt['drift'] != 'none'
        stamp = _stamp(b.primary, env['key_id'] if env else None, call, agent_changed)
        created = []
        # 3. deployment drift: one finding per client per (approved version, running version)
        if agent_changed:
            sev = binding.SEVERITY[receipt['drift']]
            dd = receipt.get('drift_detail', {})
            msg = {'renumbered': 'Agent re-published with an identical prompt and tools',
                   'prompt_changed': 'Agent prompt changed since approval',
                   'version_mismatch': 'Agent version differs from the approved version',
                   'capability_contraction': f"Agent lost approved capabilities: {', '.join(dd.get('missing_capabilities', []))}",
                   'capability_expansion': f"Agent gained unapproved capabilities: {', '.join(dd.get('unapproved_capabilities', []))}",
                   'fingerprint_mismatch': 'Same version number but the prompt/tools differ from what was approved'}[receipt['drift']]
            fid = _add_finding(db, call, client, 'agent_drift', 'open',
                               f"{msg} (agent v{call['agent_version']} running; manifest v{b.primary['version']} approved for v{body['agent']['approved_agent_version']})",
                               severity=sev, rule={'field': 'agent.approved_agent_version', 'value': body['agent']['approved_agent_version'],
                                                   'drift_class': receipt['drift'], 'detail': dd},
                               stamp=stamp, dedupe=f"drift:{client['id']}:{b.primary['sha256']}:{call['agent_version']}")
            if fid:
                created.append(fid)
        # 3b. configuration sync: does the live agent's prompt contradict the approved rules? (P1 #10)
        if running and call['source'] != 'trial':
            issues = config_issues(body, running)
            if issues:
                fid = _add_finding(db, call, client, 'config_drift', 'open',
                                   f"Live agent v{call['agent_version']} still follows old instructions: " + '; '.join(i['text'] for i in issues[:3]),
                                   severity='high', rule={'field': 'agent prompt', 'value': 'config', 'text': f"Manifest v{b.primary['version']} is the source of truth",
                                                          'issues': issues},
                                   span={'quote': issues[0].get('quote', '')}, stamp=stamp,
                                   dedupe=f"cfg:{client['id']}:{b.primary['sha256']}:{manifest_mod.agent_fingerprint(running) or call['agent_version']}")
                if fid:
                    created.append(fid)
        if not turns:
            db.update('call_state', {'call_id': call_id}, coverage='no_transcript', **state)
            return {'status': 'no_transcript'}
        # 4-5. label agent turns, apply manifest rules deterministically
        labels, backend = label(turns)
        start = parse_iso(call['started_at'])
        transfer_turns = [t['after_turn'] for t in tools if 'transfer' in (t.get('name') or '').lower()]
        primary = checks.transcript_checks(body, labels, transferred=meta.get('transferred', False),
                                           transfer_turns=transfer_turns, started_at=None if call['source'] == 'trial' else start,
                                           transfer_failed=meta.get('transfer_failed', False), turns=turns)
        others = [checks.finding_signature(checks.transcript_checks(c.j('body'), labels, transferred=meta.get('transferred', False),
                                                                    transfer_turns=transfer_turns, started_at=start,
                                                                    transfer_failed=meta.get('transfer_failed', False), turns=turns))
                  for c in b.candidates[1:]]
        for f in primary:
            note = None
            if b.status == 'boundary':
                sig = (f['type'], str(f['rule'].get('value')))
                if any(sig not in o for o in others):
                    note = (f'Call started within {config.BINDING_EPSILON_S}s of a manifest change and the adjacent version '
                            'would not flag this. Held for review.')
            fid = _add_finding(db, call, client, f['type'], 'open', f['reason'], severity=f['severity'], rule=f['rule'],
                               span=f['span'], stamp=stamp, boundary_note=note, dedupe=f"{call_id}:{f['type']}:{f['rule'].get('value') if not isinstance(f['rule'].get('value'), dict) else 'h'}")
            if fid:
                created.append(fid)
        # 5b. semantic correctness: factual claims against the approved business facts
        fx = facts_mod.check(body, turns, tools, start)
        for f in fx['findings']:
            fid = _add_finding(db, call, client, f['type'], 'open', f['reason'], severity=f['severity'], rule=f['rule'],
                               span=f['span'], stamp=stamp, dedupe=f"{call_id}:{f['type']}:{f['rule'].get('value')}")
            if fid:
                created.append(fid)
        caller_phone = meta.get('from_number') if (meta.get('direction') or 'inbound') == 'inbound' else meta.get('to_number')
        # 6. booking commitment
        c = extract_commitment(turns, tools, client['timezone'], start or now())
        for flag in c.flags:
            if flag in ('false_success', 'unrequested_booking'):
                reason = {'false_success': 'Agent told the caller the booking was done, but no successful booking tool call was recorded',
                          'unrequested_booking': c.reason or 'Agent booked without a caller request'}[flag]
                fid = _add_finding(db, call, client, flag, 'open', reason, severity=checks.SEV[flag],
                                   span={'turn': c.evidence[-1]['turn'] if c.evidence else None,
                                         'quote': c.evidence[-1]['quote'] if c.evidence else ''},
                                   rule={'field': 'record', 'value': flag, 'text': 'A tool response is evidence, not proof'},
                                   stamp=stamp, dedupe=f'{call_id}:{flag}')
                if fid:
                    created.append(fid)
        coverage = 'checked'
        if c.status in ('confirmed', 'abstain'):
            if c.action and c.action not in body.get('allowed_actions', []):
                fid = _add_finding(db, call, client, 'action_not_allowed', 'open',
                                   f"Agent performed '{c.action}', which this manifest does not allow",
                                   severity='high', rule={'field': 'allowed_actions', 'value': c.action, 'text': ', '.join(body.get('allowed_actions', []))},
                                   stamp=stamp, dedupe=f'{call_id}:action')
                if fid:
                    created.append(fid)
            tool = c.tool or {}
            cals = list(body.get('record', {}).get('calendars') or [])
            promised = {'action': c.action, 'start_at': c.start_at, 'local_text': c.local_text,
                        'contact_id': tool.get('contact_id'), 'appointment_id': tool.get('appointment_id'),
                        'calendars': cals, 'tool_calendar': tool.get('calendar_id'), 'tool_start': tool.get('tool_start'),
                        'uncertain': c.uncertain, 'evidence': c.evidence, 'corrections': c.corrections,
                        'flags': c.flags, 'how': c.reason, 'backend': c.backend, 'labeler': backend,
                        'caller_phone': caller_phone}
            if c.status == 'confirmed' and c.action in ('book', 'reschedule'):
                for rv in checks.booking_rule_checks(body, parse_iso(c.start_at) if c.start_at else None, start, turns):
                    fid = _add_finding(db, call, client, 'rule_violation', 'open', rv['reason'], severity='high', rule=rv['rule'],
                                       span={'turn': c.evidence[-1]['turn'] if c.evidence else None, 'quote': c.evidence[-1]['quote'] if c.evidence else ''},
                                       stamp=stamp, dedupe=f"{call_id}:rule:{rv['rule']['value']}")
                    if fid:
                        created.append(fid)
            if c.status == 'confirmed':
                missing_ids = not tool.get('contact_id') and not tool.get('appointment_id')
                reason = 'Awaiting first calendar read' + (' · agent read back a different time than the caller asked' if 'readback_mismatch' in c.flags else '')
                fid = _add_finding(db, call, client, 'booking', 'unknown' if missing_ids else 'pending',
                                   'No contact or appointment ID in the call, so the calendar cannot be checked' if missing_ids else reason,
                                   severity='high', promised=promised, stamp=stamp, appointment_at=c.start_at,
                                   rule={'field': 'record', 'value': c.action, 'text': 'Booking landed in HighLevel'},
                                   span={'turn': c.evidence[0]['turn'] if c.evidence else None, 'quote': c.evidence[0]['quote'] if c.evidence else ''},
                                   dedupe=f'{call_id}:booking')
                if fid:
                    created.append(fid)
                    _supersede(db, client, fid, promised)
                    if missing_ids:
                        cases.ensure_case(db, fid)
                    else:
                        fr = db.one('SELECT * FROM findings WHERE id=?', fid)
                        schedule_reads(db, fr, parse_iso(call['ended_at']) or now(),
                                       parse_iso(c.start_at) if c.start_at else None,
                                       body.get('check_timing', {}).get('reads_min', [2, 15, 60]))
            else:
                fid = _add_finding(db, call, client, 'booking', 'unknown', c.reason, severity='medium', promised=promised,
                                   stamp=stamp, rule={'field': 'record', 'value': c.action, 'text': 'Booking landed in HighLevel'},
                                   span={'turn': c.evidence[0]['turn'] if c.evidence else None, 'quote': c.evidence[0]['quote'] if c.evidence else ''},
                                   dedupe=f'{call_id}:booking')
                if fid:
                    created.append(fid)
                    cases.ensure_case(db, fid)
                coverage = 'booking_unknown'
        # 6a. every caller request must end in a verifiable outcome (P0 #6)
        reqs = intents.detect(turns)
        transferred_ok = meta.get('transferred', False)
        promised_cb = [l for l in labels if l.kind == 'promise' and l.cls in ('callback', 'staff_message')]
        contact_for = (c.tool or {}).get('contact_id')
        if 'request_unhandled' in c.flags and not transferred_ok and not promised_cb:
            fid = _add_finding(db, call, client, 'incomplete_request', 'open',
                               f"Caller asked to {c.action or 'book'} an appointment; the call ended with no {c.action or 'booking'}, no transfer and no callback",
                               severity='high', rule={'field': 'requests', 'value': 'appointment', 'text': 'Every caller request ends in an outcome'},
                               span={'turn': c.evidence[0]['turn'] if c.evidence else None, 'quote': c.evidence[0]['quote'] if c.evidence else ''},
                               stamp=stamp, dedupe=f'{call_id}:incomplete:appointment')
            if fid:
                created.append(fid)
        for r in reqs:
            if r['kind'] in ('update_email', 'update_phone'):
                fid = _add_finding(db, call, client, 'contact_update', 'pending', 'Awaiting first contact read', severity='high',
                                   promised={'field': r['field'], 'value': r['value'], 'contact_id': contact_for, 'caller_phone': caller_phone},
                                   span={'turn': r['turn'], 'quote': r['quote']}, stamp=stamp, dedupe=f"{call_id}:contact:{r['field']}",
                                   rule={'field': 'requests', 'value': r['kind'], 'text': f"Caller's new {r['field']} is saved in the CRM"})
                if fid:
                    created.append(fid)
                    schedule_reads(db, db.one('SELECT * FROM findings WHERE id=?', fid), parse_iso(call['ended_at']) or now(), None,
                                   body.get('check_timing', {}).get('reads_min', [2, 15, 60]))
            elif r['kind'] == 'callback_request' and not promised_cb and not transferred_ok and \
                    'callback' in body.get('deliverable_promises', []):
                fid = _add_finding(db, call, client, 'incomplete_request', 'open',
                                   'Caller asked for a callback; the agent neither promised one nor transferred the call', severity='high',
                                   rule={'field': 'requests', 'value': 'callback_request', 'text': 'Every caller request ends in an outcome'},
                                   span={'turn': r['turn'], 'quote': r['quote']}, stamp=stamp, dedupe=f'{call_id}:incomplete:callback')
                if fid:
                    created.append(fid)
        # 6c. promised callbacks the setup can deliver: a task must exist and be completed in time (P0 #4)
        cbs = [l for l in promised_cb if l.cls in body.get('deliverable_promises', [])]
        if cbs:
            sla = int(body.get('callback_sla_hours') or 4)
            end_at = parse_iso(call['ended_at']) or now()
            fid = _add_finding(db, call, client, 'callback_promised', 'pending', 'Awaiting first task read', severity='high',
                               promised={'contact_id': contact_for, 'caller_phone': caller_phone, 'due_at': iso(end_at + timedelta(hours=sla)),
                                         'sla_hours': sla, 'kind': cbs[0].cls},
                               span={'turn': cbs[0].turn, 'quote': cbs[0].quote}, stamp=stamp, dedupe=f'{call_id}:callback',
                               rule={'field': 'deliverable_promises', 'value': cbs[0].cls, 'text': f'A callback task exists and is completed within {sla}h'})
            if fid:
                created.append(fid)
                reads = sorted(set(body.get('check_timing', {}).get('reads_min', [2, 15, 60]) + [sla * 60]))
                schedule_reads(db, db.one('SELECT * FROM findings WHERE id=?', fid), end_at, None, reads)
        # 6b. promised text confirmations the setup *can* deliver: did exactly one actually go out?
        sms = [l for l in labels if l.kind == 'promise' and l.cls == 'sms_confirmation']
        if sms and 'sms_confirmation' in body.get('deliverable_promises', []):
            tool = (c.tool or {}) if c.status in ('confirmed', 'abstain') else {}
            fid = _add_finding(db, call, client, 'message_promised', 'pending', 'Awaiting first message read', severity='medium',
                               promised={'channel': 'sms', 'contact_id': tool.get('contact_id'), 'caller_phone': caller_phone},
                               span={'turn': sms[0].turn, 'quote': sms[0].quote}, stamp=stamp, dedupe=f'{call_id}:message',
                               rule={'field': 'deliverable_promises', 'value': 'sms_confirmation', 'text': 'Promised text confirmation is sent once'})
            if fid:
                created.append(fid)
                fr = db.one('SELECT * FROM findings WHERE id=?', fid)
                schedule_reads(db, fr, parse_iso(call['ended_at']) or now(), None,
                               body.get('check_timing', {}).get('reads_min', [2, 15, 60]))
        db.update('call_state', {'call_id': call_id}, coverage=coverage, **state)
        return {'status': 'processed', 'findings': created, 'binding': b.status, 'drift': receipt['drift']}


def _supersede(db: Database, client, new_id: int, promised: dict):
    """A later call that replaced the request closes the older open booking finding, linked to the new one."""
    if not promised.get('contact_id'):
        return
    from . import cases
    rows = db.all("SELECT * FROM findings WHERE client_id=? AND type='booking' AND id<>? AND state IN ('pending','contradicted','unknown')",
                  client['id'], new_id)
    for r in rows:
        p = r.j('promised', {})
        same = p.get('contact_id') == promised['contact_id'] and (
            promised.get('action') in ('reschedule', 'cancel') or
            (p.get('appointment_id') and p.get('appointment_id') == promised.get('appointment_id')))
        if same:
            db.update('findings', {'id': r['id']}, state='superseded', superseded_by=new_id,
                      reason=f'Replaced by a later request (finding #{new_id})', updated_at=iso(now()))
            db.run("UPDATE jobs SET done=1 WHERE kind='recheck' AND ref_id=? AND done=0", r['id'])
            c = db.one('SELECT id FROM cases WHERE finding_id=?', r['id'])
            if c:
                db.update('cases', {'id': c['id']}, status='superseded', closed_at=iso(now()), updated_at=iso(now()))
                cases.event(db, c['id'], 'vaada', 'superseded', by_finding=new_id)
