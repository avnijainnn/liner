"""Score Vaada against the labelled synthetic datasets in eval/data.

Run:  python -m eval.run_eval      (from the project root)
Every dataset is SYNTHETIC. These scores measure the detection logic, not real-world prevalence.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.extract import extract
from app.verify import parse_iso
from app.verify import compare

D = Path(__file__).parent / 'data'
REF = datetime(2026, 10, 22, 14, tzinfo=timezone.utc)
WORLD = json.loads((D / 'agency_world.json').read_text())
CLIENTS = {c['key']: c for c in WORLD['clients']}


def jl(name):
    return [json.loads(l) for l in (D / name).read_text().splitlines() if l.strip()]


def turns(transcript):
    parts = re.split(r'(?:^|\s)(Caller|Agent):\s*', transcript.strip())
    out = []
    for role, text in zip(parts[1::2], parts[2::2]):
        out.append({'i': len(out), 'role': role.lower(), 'text': text.strip()})
    return out


def same_start(a, b):
    if a is None or b is None:
        return a is None and b is None
    return parse_iso(a) == parse_iso(b)


def commit(transcript, tz='America/Chicago', ref=REF):
    c = extract(turns(transcript), [], tz, ref)
    return c


def booking_state(transcript, contact, reads, calendar):
    """reads: list of records (or None) in time order, as Vaada's scheduled reads would see them."""
    c = commit(transcript)
    if c.status != 'confirmed':
        return 'none' if c.status == 'none' else 'unknown', c
    if not contact:
        return 'unknown', c
    if not reads:
        return 'contradicted', c
    state = 'pending'
    for i, rec in enumerate(reads):
        r = None
        if rec is not None:
            r = {'contact_id': rec.get('contactId', contact), 'calendar_id': rec.get('calendarId', calendar),
                 'start_at': rec.get('startTime'), 'status': rec.get('status')}
        ok, why, unk = compare({'action': c.action or 'book', 'start_at': c.start_at, 'contact_id': contact}, r, [calendar])
        last = i == len(reads) - 1
        if ok:
            return 'verified', c
        state = 'unknown' if unk else ('contradicted' if last else 'pending')
    return state, c


def score(name, rows):
    ok = sum(r[1] == r[2] for r in rows)
    print(f'\n{name}: {ok}/{len(rows)} correct')
    for rid, want, got, note in rows:
        if want != got:
            print(f'  MISS {rid}: expected {want}, got {got}  {note}')
    return ok, len(rows)


def main():
    totals = []
    # 1. extraction
    for fn in ('extraction_challenge.jsonl', 'extraction_extended_synthetic.jsonl'):
        rows = []
        for r in jl(fn):
            ref = datetime.fromisoformat(r['call_started_at'].replace('Z', '+00:00'))
            c = commit(r['transcript'], r['timezone'], ref)
            want = (r['commitment']['confirmed'], r['commitment']['final_start'])
            got_conf = c.status == 'confirmed'
            good = got_conf == want[0] and (not want[0] or same_start(c.start_at, want[1]))
            rows.append((r['id'], 'ok', 'ok' if good else 'wrong', f'(got {c.status} {c.start_at}; {c.reason})'))
        totals.append(score(f'Extraction · {fn}', rows))
    # 2. booking verification
    rows = []
    for r in jl('booking_cases.jsonl'):
        reads = [r['record']] + ([r['after_fix']] if 'after_fix' in r else [])
        got, c = booking_state(r['transcript'], r['contact_id'], reads, 'cal_dataset')
        rows.append((r['id'], r['expected_state'], got, f'({c.reason})'))
    totals.append(score('Booking verification · booking_cases.jsonl', rows))
    # 3. accountability regression: booking state, drift, permission
    rows, by_cat = [], defaultdict(Counter)
    for r in jl('accountability_regression_72.jsonl'):
        cl = CLIENTS[r['client']]
        pol = cl['policy']
        got, c = booking_state(r['transcript'], r['contact_id'], [e['record'] for e in r['calendar_events']], cl['calendar'])
        drift = str(r['agent_version']) != pol['approved_agent_version']
        allowed = (c.action or 'book') in pol['allowed_actions'] if c.status == 'confirmed' else True
        res = [(r['id'] + ':booking', r['expected_booking_state'], got),
               (r['id'] + ':drift', r['expected_agent_drift'], drift),
               (r['id'] + ':permission', r['expected_permission'], 'allowed' if allowed else 'not_allowed'),
               (r['id'] + ':policy_flag', r['expected_policy_flag'], not allowed)]
        for rid, want, g in res:
            rows.append((rid, want, g, ''))
            by_cat[r['category']]['ok' if want == g else 'miss'] += 1
    totals.append(score('Accountability regression · 72 calls × 4 checks', rows))
    for cat, cnt in sorted(by_cat.items()):
        print(f'  {cat:22s} {cnt["ok"]}/{cnt["ok"] + cnt["miss"]}')
    # 4. agency world
    rows = []
    expect = {'world_wrong_time': ('contradicted', False, True), 'world_correct_booking': ('verified', False, True),
              'world_delayed_write': ('verified', False, True), 'world_agent_drift': ('verified', True, True),
              'world_availability_only': ('none', False, True), 'world_unauthorized_booking': ('verified', False, False)}
    for call in WORLD['calls']:
        cl = CLIENTS[call['client']]
        got, c = booking_state(call['transcript'], call['contact'], [t['record'] for t in call['timeline']], cl['calendar'])
        drift = call['agent_version'] != cl['policy']['approved_agent_version']
        allowed = (c.action or 'book') in cl['policy']['allowed_actions'] if c.status == 'confirmed' else True
        rows.append((call['id'], expect[call['id']], (got, drift, allowed), ''))
    totals.append(score('Agency world · 3 clients, 6 calls (booking state, drift, permission)', rows))
    # 5. trial responses: rule checks on the canned agent replies
    from app.labeler import label
    rows = []
    for ck, scs in json.loads((D / 'trial_responses.json').read_text()).items():
        pol = CLIENTS[ck]['policy']
        for sid, runs in scs.items():
            kind, _, rule = sid.partition(':')
            for i, run in enumerate(runs):
                labs = label(turns(run["transcript"]))[0]
                if kind == 'never_ask':
                    got = 'fail' if any(l.kind == 'ask' and l.cls == rule for l in labs) else 'pass'
                    want = 'fail' if re.search(r'tell me your card|what is your|give me your', run['transcript'], re.I) else 'pass'
                elif kind == 'never_promise':
                    got = 'fail' if any(l.kind == 'promise' and l.cls == rule for l in labs) else 'pass'
                    want = 'pass'
                elif kind == 'deliverable':
                    got = 'pass' if rule in run.get('capabilities_executed', []) else 'fail'
                    want = 'pass' if run.get('capabilities_executed') else 'fail'
                elif kind == 'handoff':
                    got = want = 'pass' if run.get('transfer_event') else 'fail'
                else:  # action:book
                    exp, obs = run['expected_record'], run['observed_record']
                    m, _, _ = compare({'action': 'book', 'start_at': exp['final_start'], 'contact_id': exp['contact_id']},
                                      {'contact_id': obs['contactId'], 'calendar_id': obs['calendarId'], 'start_at': obs['startTime'], 'status': obs['status']},
                                      [exp['calendar_id']])
                    got = 'pass' if m else 'fail'
                    want = 'pass' if same_start(exp['final_start'], obs['startTime']) else 'fail'
                rows.append((f'{ck}/{sid}#{i + 1}', want, got, run['transcript'].replace('\n', ' | ')))
    totals.append(score('Trial responses · canned agent replies', rows))
    ok, n = map(sum, zip(*totals))
    print(f'\nTOTAL {ok}/{n} = {ok / n:.1%}  (all data SYNTHETIC)')


if __name__ == '__main__':
    main()
