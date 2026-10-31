"""End-to-end connector test: Google Schema-Guided Dialogue (SGD) bookings -> Vapi webhooks -> Vaada -> HighLevel API (mock).

SGD (Rastogi et al., 2020; CC BY-SA 4.0) has human-written appointment dialogues for hair stylists
(Services_1) and therapists (Services_4). Every BookAppointment service call carries the exact date and
time booked, which is the gold label for "what the caller finally agreed". Dialogues are set around
Friday 2019-03-01, so they are shifted by whole weeks to the next Friday (weekday words stay true) and
spoken "March 7th" / "the 7th" dates are rewritten to the shifted date.

Each dialogue is replayed as a Vapi end-of-call-report, signed with HMAC, POSTed to Vaada's real
/webhooks/vapi endpoint, processed, and checked through Vaada's real HighLevel client against a mock
HighLevel API. A fault is injected per call so every verdict has a known right answer:

  correct          record matches the call                          -> verified
  wrong_time       agent wrote the slot one hour late               -> contradicted
  wrong_date       agent wrote the next day                         -> contradicted
  dropped          tool said OK, record never saved                 -> contradicted
  false_success    tool failed, agent still said booked             -> contradicted (+ false_success)
  wrong_contact    booked under another contact                     -> contradicted
  cancelled        record exists but is cancelled                   -> contradicted
  honest_failure   SGD's own failed bookings: agent says it failed  -> no booking finding

Run:  python -m eval.sgd_replay /path/to/dstc8-schema-guided-dialogue [--n 200]
All calls, contacts and records are synthetic.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = 'America/Los_Angeles'
SECRET = 'vapi-test-secret'
HL_TOKEN = 'hl-test-token'
LOC = 'loc_sgd'
CAL = 'cal_sgd'
SGD_REF = date(2019, 3, 1)   # Friday; SGD's "today"
FAULTS = ['correct', 'correct', 'wrong_time', 'wrong_date', 'dropped', 'false_success', 'wrong_contact', 'cancelled']
MONTH = re.compile(r'\b(?:March|Mar\.?)\s+(\d{1,2})(?:st|nd|rd|th)?\b', re.I)
OF_MONTH = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)\s+of\s+(?:March|this\s+month)\b', re.I)
THE_DAY = re.compile(r'\bthe\s+(\d{1,2})(st|nd|rd|th)\b', re.I)


def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def next_friday(d: date) -> date:
    return d + timedelta(days=(4 - d.weekday()) % 7 or 7)


class Shift:
    def __init__(self, base: date):
        self.base = base

    def day(self, sgd_day: int) -> date:
        return self.base + (date(2019, 3, sgd_day) - SGD_REF)

    def say(self, d: date) -> str:
        return f"{d.strftime('%B')} {ordinal(d.day)}"

    def text(self, s: str) -> str:
        s = MONTH.sub(lambda m: self.say(self.day(int(m.group(1)))), s)
        s = OF_MONTH.sub(lambda m: self.say(self.day(int(m.group(1)))), s)
        return THE_DAY.sub(lambda m: self.say(self.day(int(m.group(1)))) if 1 <= int(m.group(1)) <= 31 else m.group(0), s)


def load_bookings(sgd_dir: str):
    out = []
    for f in sorted(glob.glob(os.path.join(sgd_dir, '*', 'dialogues_*.json'))):
        for d in json.load(open(f)):
            if not any(s in ('Services_1', 'Services_4') for s in d['services']):
                continue
            calls = [fr['service_call'] for t in d['turns'] for fr in t['frames'] if fr.get('service_call')]
            # one unambiguous label: exactly one booking call, and no other transactional call (restaurants, cabs, ...)
            if sum(c['method'] == 'BookAppointment' for c in calls) != 1 or any(c['method'] not in ('BookAppointment', 'FindProvider') for c in calls):
                continue
            for ti, t in enumerate(d['turns']):
                for fr in t['frames']:
                    sc = fr.get('service_call')
                    if sc and sc['method'] == 'BookAppointment' and fr['service'] in ('Services_1', 'Services_4'):
                        said_failed = bool(re.search(r"couldn'?t|could not|unable|not able|sorry|failed|went wrong|unsuccessful|isn'?t available|not available|instead\?", t['utterance'], re.I))
                        out.append({'dialogue': d, 'turn': ti, 'call': sc, 'ok': bool(fr.get('service_results')) and not said_failed, 'service': fr['service']})
    return out


def build(case, idx, shift: Shift, call_start: datetime, fault: str):
    d, bt, sc = case['dialogue'], case['turn'], case['call']
    p = sc['parameters']
    gday = shift.day(int(p['appointment_date'][-2:]))
    hh, mm = map(int, p['appointment_time'].split(':'))
    gold = datetime(gday.year, gday.month, gday.day, hh, mm, tzinfo=ZoneInfo(TZ))
    contact = f'ct_sgd_{idx:04d}'
    phone = f'+1415555{idx:04d}'
    appt = f'appt_sgd_{idx:04d}'
    written = gold + timedelta(hours=1) if fault == 'wrong_time' else gold + timedelta(days=1) if fault == 'wrong_date' else gold
    tool_ok = case['ok'] and fault != 'false_success'
    msgs, t0 = [{'role': 'system', 'message': 'You are the booking assistant.', 'time': 0, 'secondsFromStart': 0}], call_start.timestamp() * 1000
    for i, t in enumerate(d['turns']):
        sec = 4 * len(msgs)
        if i == bt:
            tcid = f'call_{idx}_book'
            args = {'contactId': contact, 'calendarId': CAL, 'startTime': written.isoformat(), 'title': p.get('stylist_name') or p.get('therapist_name')}
            msgs.append({'role': 'tool_calls', 'message': '', 'time': t0 + sec * 1000, 'secondsFromStart': sec,
                         'toolCalls': [{'id': tcid, 'type': 'function', 'function': {'name': 'book_appointment', 'arguments': json.dumps(args)}}]})
            res = {'id': appt, 'status': 'booked'} if tool_ok else {'error': 'slot unavailable'}
            msgs.append({'role': 'tool_call_result', 'toolCallId': tcid, 'name': 'book_appointment', 'result': json.dumps(res),
                         'time': t0 + (sec + 1) * 1000, 'secondsFromStart': sec + 1})
        text = shift.text(t['utterance'])
        msgs.append({'role': 'user' if t['speaker'] == 'USER' else 'bot', 'message': text, 'time': t0 + (sec + 2) * 1000, 'secondsFromStart': sec + 2})
    end = call_start + timedelta(seconds=4 * len(msgs) + 10)
    payload = {'message': {'type': 'end-of-call-report', 'endedReason': 'customer-ended-call', 'assistantVersion': '3',
                           'startedAt': call_start.isoformat(), 'endedAt': end.isoformat(),
                           'customer': {'number': phone}, 'phoneNumber': {'number': '+14155550000'},
                           'call': {'id': f'vapi_sgd_{idx:04d}_{int(time.time())}', 'assistantId': 'asst_sgd_booking', 'type': 'inboundPhoneCall'},
                           'artifact': {'messages': msgs}}}
    rec = None
    if case['ok'] and fault not in ('dropped', 'false_success'):
        rec = {'id': appt, 'contactId': 'ct_someone_else' if fault == 'wrong_contact' else contact, 'calendarId': CAL, 'locationId': LOC,
               'startTime': written.isoformat(), 'endTime': (written + timedelta(hours=1)).isoformat(),
               'appointmentStatus': 'cancelled' if fault == 'cancelled' else 'confirmed', 'dateAdded': end.isoformat()}
    expect = 'none' if not case['ok'] else ('verified' if fault == 'correct' else 'contradicted')
    return payload, rec, {'id': contact, 'locationId': LOC, 'phone': phone, 'firstName': f'Caller {idx}'}, gold, expect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sgd_dir')
    ap.add_argument('--n', type=int, default=200)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--port', type=int, default=8771)
    ap.add_argument('--hl-port', type=int, default=8799)
    ap.add_argument('--out', default='eval/sgd_results.json')
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix='vaada_sgd_'))
    hl_data = work / 'hl.json'
    env = {**os.environ, 'VAADA_SECRET_KEY': 'sgd-replay', 'DATABASE_URL': f'sqlite:///{work}/vaada.db', 'VAADA_DEMO_MODE': 'false',
           'VAADA_INLINE_WORKER': 'false', 'HIGHLEVEL_BASE': f'http://127.0.0.1:{a.hl_port}', 'VAADA_PUBLIC_URL': f'http://127.0.0.1:{a.port}',
           'MOCK_HL_DATA': str(hl_data), 'MOCK_HL_TOKEN': HL_TOKEN}
    os.environ.update(env)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import accounts, crypto, manifest, pipeline, verify
    from app.db import get_db
    from app.http import post, get

    db = get_db()
    aid = accounts.create_agency(db, 'Replay Agency (SYNTHETIC)', 'replay@vaada.dev', 'Replay', 'replay-pass-2026')
    db.update('agencies', {'id': aid}, vapi_secret_enc=crypto.encrypt(SECRET))
    key = db.val('SELECT webhook_key FROM agencies WHERE id=?', aid)
    cid = accounts.create_client(db, aid, name='Shear & Calm (SYNTHETIC)', retell_agent_id='asst_sgd_booking', timezone=TZ,
                                 hl_location_id=LOC, calendars=[{'id': CAL, 'name': 'Appointments'}], hl_token=HL_TOKEN)
    pol = dict(manifest.DEFAULT_POLICY)
    pol['business_hours'] = {d: ['07:00', '21:00'] for d in manifest.DAYS}
    pol['allowed_actions'] = ['book']
    draft = manifest.create_draft(db, cid, pol, by='replay')
    manifest.approve(db, draft, approver='Replay Owner', role='agency', method='replay', approved_agent_version='3')

    world = {'locations': {LOC: {'id': LOC, 'name': 'Shear & Calm', 'timezone': TZ}}, 'contacts': {}, 'appointments': {}, 'tasks': {},
             'calendars': [{'id': CAL, 'locationId': LOC, 'name': 'Appointments', 'slotDuration': 60}]}
    hl_data.write_text(json.dumps(world))
    procs = [subprocess.Popen([sys.executable, '-m', 'uvicorn', 'eval.mock_highlevel:app', '--port', str(a.hl_port), '--log-level', 'warning'], env=env),
             subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.web:app', '--port', str(a.port), '--log-level', 'warning'], env=env)]
    try:
        for _ in range(60):
            try:
                if get(f'http://127.0.0.1:{a.port}/health', timeout=2).status_code == 200 and get(f'http://127.0.0.1:{a.hl_port}/__stats', timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        from app.integrations import vapi
        cases = load_bookings(a.sgd_dir)
        rng = random.Random(a.seed)
        rng.shuffle(cases)
        cases = cases[:a.n]
        shift = Shift(next_friday(datetime.now(ZoneInfo(TZ)).date()))
        call_start = datetime.combine(shift.base, datetime.min.time(), ZoneInfo(TZ)).replace(hour=9)
        rows = []
        # 1. one unsigned and one wrongly-signed post must be rejected
        bad = json.dumps({'message': {'type': 'end-of-call-report'}}).encode()
        r1 = post(f'http://127.0.0.1:{a.port}/webhooks/vapi/{key}', data=bad, headers={'content-type': 'application/json'})
        r2 = post(f'http://127.0.0.1:{a.port}/webhooks/vapi/{key}', data=bad, headers={'content-type': 'application/json', **vapi.sign(bad, 'wrong')})
        auth_ok = r1.status_code == 401 and r2.status_code == 401
        for idx, case in enumerate(cases):
            fault = FAULTS[idx % len(FAULTS)] if case['ok'] else 'honest_failure'
            payload, rec, contact, gold, expect = build(case, idx, shift, call_start + timedelta(minutes=idx), fault)
            world['contacts'][contact['id']] = contact
            if rec:
                world['appointments'][rec['id']] = rec
            hl_data.write_text(json.dumps(world))
            body = json.dumps(payload).encode()
            r = post(f'http://127.0.0.1:{a.port}/webhooks/vapi/{key}', data=body, headers={'content-type': 'application/json', **vapi.sign(body, SECRET)})
            res = r.json()
            row = {'idx': idx, 'service': case['service'], 'fault': fault, 'expect': expect, 'gold': gold.isoformat(), 'http': r.status_code}
            if res.get('status') != 'ingested':
                row.update(got='ingest_failed', detail=res)
                rows.append(row)
                continue
            pipeline.process_call(db, res['call_id'])
            fs = db.all('SELECT * FROM findings WHERE call_id=?', res['call_id'])
            b = next((f for f in fs if f['type'] == 'booking'), None)
            if b and b['state'] == 'pending':
                with db.tx():
                    b = verify.check(db, b['id'], 'final')
            p = (b.j('promised', {}) if hasattr(b, 'j') else json.loads(b['promised'])) if b else {}
            got_start = p.get('start_at')
            row.update(got=b['state'] if b else 'none', reason=b['reason'] if b else '', backend=p.get('backend'), flags=p.get('flags') or [],
                       extracted=got_start, extract_ok=bool(got_start) and datetime.fromisoformat(got_start.replace('Z', '+00:00')) == gold,
                       types=sorted({f['type'] for f in fs}),
                       transcript=[m['message'] for m in payload['message']['artifact']['messages'] if m['role'] in ('user', 'bot')])
            rows.append(row)
        hl_stats = get(f'http://127.0.0.1:{a.hl_port}/__stats').json()
    finally:
        for pr in procs:
            pr.terminate()

    # ---- report
    n = len(rows)
    correct = sum(r['got'] == r['expect'] for r in rows)
    booked = [r for r in rows if r['expect'] != 'none']
    ext = sum(r.get('extract_ok') for r in booked)
    by = defaultdict(Counter)
    for r in rows:
        by[r['fault']]['ok' if r['got'] == r['expect'] else 'miss'] += 1
    fs_hit = sum('false_success' in r.get('types', []) for r in rows if r['fault'] == 'false_success')
    fs_n = sum(r['fault'] == 'false_success' for r in rows)
    caught = sum(r['got'] == 'contradicted' for r in rows if r['expect'] == 'contradicted')
    bad_total = sum(r['expect'] == 'contradicted' for r in rows)
    false_alarm = sum(r['got'] == 'contradicted' for r in rows if r['expect'] != 'contradicted')
    flagged = [r for r in rows if r['got'] == 'contradicted']
    precision = sum(r['expect'] == 'contradicted' for r in flagged) / max(1, len(flagged))
    good = [r for r in rows if r['expect'] == 'verified']
    accepted = sum(r['got'] == 'verified' for r in good)
    review = sum('single_reader' in r.get('flags', []) for r in rows)
    disagree = sum('readers_disagree' in r.get('flags', []) for r in rows)
    unavailable = sum('model_unavailable' in r.get('flags', []) for r in rows)
    print(f'SGD replay through Vapi webhook -> Vaada -> HighLevel API (mock)  ·  {n} calls  ·  all SYNTHETIC')
    print(f'  webhook auth: unsigned and bad-signature posts rejected: {auth_ok}')
    print(f'  HighLevel API requests made by Vaada: {hl_stats["requests"]}')
    print(f'  verdict accuracy: {correct}/{n} = {correct / n:.1%}')
    print(f'  faults caught: {caught}/{bad_total} · false alarms on good or failed-honestly calls: {false_alarm}/{n - bad_total}')
    print(f'  flags that are correct (precision): {precision:.1%} · correct bookings accepted: {accepted}/{len(good)}')
    print(f'  reader: {os.environ.get("VAADA_EXTRACTOR", "rules")} · single-reader results (send to review): {review} · readers disagreed (abstained): {disagree} · no model answer (rate limit, error or failed check): {unavailable}')
    print(f'  date+time extracted exactly from human-written dialogue: {ext}/{len(booked)} = {ext / max(1, len(booked)):.1%}')
    print(f'  false_success also flagged: {fs_hit}/{fs_n}')
    for k, c in sorted(by.items()):
        print(f'    {k:15s} {c["ok"]}/{c["ok"] + c["miss"]}')
    misses = Counter(r['got'] + ' | ' + (r.get('reason') or '')[:70] for r in rows if r['got'] != r['expect'])
    if misses:
        print('  misses by outcome:')
        for k, v in misses.most_common(8):
            print(f'    {v:3d}  {k}')
    Path(a.out).write_text(json.dumps({'summary': {'calls': n, 'verdict_accuracy': correct / n, 'faults_caught': [caught, bad_total],
                                                   'false_alarms': [false_alarm, n - bad_total], 'extraction_exact': [ext, len(booked)],
                                                   'webhook_auth_ok': auth_ok, 'precision': precision,
                                                   'accepted_correct': [accepted, len(good)], 'single_reader': review, 'disagree': disagree, 'model_unavailable': unavailable,
                                                   'reader': os.environ.get('VAADA_EXTRACTOR', 'rules'), 'mode': os.environ.get('VAADA_EXTRACT_MODE', 'vote')}, 'rows': rows}, indent=1, default=str))
    print(f'  details: {a.out}')


if __name__ == '__main__':
    main()
