"""Held-out test #2: Google Taskmaster-1 auto-repair appointments (spoken, CC BY 4.0).

The 699 two-person "woz" auto-repair dialogues were spoken by real people; the caller side carries speech
recognition errors. Annotators marked the date and time spans the two sides finally agreed on
(`auto_repair.date.appt.accept`, `auto_repair.time.appt.accept`). Gold = the last accepted date span and the
last accepted time span. Dialogues have no call date, so every call is placed on Monday 2026-11-02 09:00 and
relative dates ("next Friday") are resolved from there, for gold and for Vaada alike.

Gold spans Vaada's own parser cannot turn into a date or time ("first thing", "after lunch") are counted
separately rather than silently dropped. This measures reading only (no record check).

  python -m eval.taskmaster_eval /path/to/Taskmaster/TM-1-2019/woz-dialogs.json
  VAADA_EXTRACTOR=anthropic ANTHROPIC_API_KEY=... python -m eval.taskmaster_eval ...   # rules + model, voting
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from app.extract import extract_commitment
from app.timeparse import combine, find_dates, find_times

TZ = 'America/Chicago'
REF = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo(TZ))


def gold(d):
    dates, times = [], []
    for u in d['utterances']:
        for s in u.get('segments', []):
            for a in s['annotations']:
                if a['name'] == 'auto_repair.date.appt.accept':
                    dates.append(s['text'])
                elif a['name'] == 'auto_repair.time.appt.accept':
                    times.append(s['text'])
    if not dates and not times:
        return 'none', None
    if not dates or not times:
        return 'partial', None
    ds, ts = find_dates(dates[-1], REF.date()), find_times(times[-1])
    if not ds or not ts:
        return 'unparseable', (dates[-1], times[-1])
    t = ts[-1]
    hour = t.hour if t.meridiem else (t.hour + 12 if t.hour < 7 else t.hour)   # shop hours: "drop off at 4" means 4 pm
    return 'ok', combine(ds[-1].value, hour % 24, t.minute, TZ)


def main():
    path = sys.argv[1]
    ds = [d for d in json.load(open(path)) if 'auto' in d['instruction_id']]
    stats, rows = Counter(), []
    for d in ds:
        turns = [{'i': i, 'role': 'caller' if u['speaker'] == 'USER' else 'agent', 'text': u['text']} for i, u in enumerate(d['utterances'])]
        kind, g = gold(d)
        stats['gold_' + kind] += 1
        tools = [{'name': 'book_appointment', 'arguments': {}, 'result': {'id': 'x'}, 'successful': True, 'after_turn': len(turns) - 2}]
        c = extract_commitment(turns, tools, TZ, REF)
        got = datetime.fromisoformat(c.start_at.replace('Z', '+00:00')) if c.start_at else None
        if kind == 'ok':
            if c.status != 'confirmed':
                stats['missed_' + c.status] += 1
            elif got == g:
                stats['exact'] += 1
            else:
                gl, ol = g.astimezone(ZoneInfo(TZ)), got.astimezone(ZoneInfo(TZ))
                stats['wrong_date' if gl.date() != ol.date() else 'wrong_time'] += 1
                rows.append({'id': d['conversation_id'], 'gold': gl.isoformat(), 'vaada': ol.isoformat(), 'flags': c.flags})
        if kind == 'none':
            stats['none_and_lane_' + c.status] += 1
        if 'single_reader' in c.flags:
            stats['single_reader'] += 1
        if 'readers_disagree' in c.flags:
            stats['readers_disagree'] += 1
    ok = stats['gold_ok']
    stated = stats['exact'] + stats['wrong_date'] + stats['wrong_time']
    print(f"Taskmaster-1 auto-repair (spoken, ASR errors) · {len(ds)} dialogues · reader: {os.environ.get('VAADA_EXTRACTOR', 'rules')}")
    print(f"  gold machine-readable: {ok} · gold unparseable: {stats['gold_unparseable']} · only date or time: {stats['gold_partial']} · no agreement: {stats['gold_none']}")
    print(f"  of {ok} gold bookings: exact {stats['exact']} ({stats['exact'] / max(1, ok):.1%}) · wrong time {stats['wrong_time']} · wrong date {stats['wrong_date']}"
          f" · no confirmation found {stats['missed_none']} · abstained {stats['missed_abstain']}")
    print(f"  when Vaada states a time, it is right {stats['exact'] / max(1, stated):.1%} of the time ({stats['exact']}/{stated})")
    if stats['single_reader'] or stats['readers_disagree']:
        print(f"  single-reader (review): {stats['single_reader']} · readers disagreed: {stats['readers_disagree']}")
    json.dump({'stats': stats, 'wrong': rows}, open('eval/taskmaster_results.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
