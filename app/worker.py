"""Durable job runner on the jobs table: call processing, timed calendar
re-reads (2 / 15 / 60 min) and trial runs. Run as `python -m app.worker`, or
in-process (VAADA_INLINE_WORKER=true)."""
from __future__ import annotations

import threading
import time
import traceback
from datetime import timedelta

from . import pipeline, trials, verify
from .db import Database, get_db, iso, now


def claim(db: Database, limit=20):
    with db.tx():
        rows = db.all('SELECT * FROM jobs WHERE done=0 AND run_at<=? AND (locked_until IS NULL OR locked_until<?) ORDER BY run_at LIMIT ?',
                      iso(now()), iso(now()), limit)
        for r in rows:
            db.update('jobs', {'id': r['id']}, locked_until=iso(now() + timedelta(minutes=5)))
    return rows


def run_job(db: Database, j: dict):
    if j['kind'] == 'process':
        pipeline.process_call(db, j['ref_id'])
        trials.evaluate_trial_call(db, j['ref_id'])
    elif j['kind'] == 'recheck':
        with db.tx():
            verify.check(db, j['ref_id'], j['stage'])
    elif j['kind'] == 'trial':
        t = db.one('SELECT mode FROM trials WHERE id=?', j['ref_id'])
        (trials.run_sandbox_trial if t['mode'] == 'sandbox' else trials.run_live_trial)(db, j['ref_id'])


def tick(db: Database | None = None) -> int:
    db = db or get_db()
    n = 0
    for j in claim(db):
        try:
            run_job(db, j)
            db.update('jobs', {'id': j['id']}, done=1, locked_until=None)
        except Exception as e:
            traceback.print_exc()
            att = j['attempts'] + 1
            with db.tx():
                if att >= 5:
                    db.update('jobs', {'id': j['id']}, done=1, attempts=att, last_error=f'{type(e).__name__}: {e}'[:300])
                    if j['kind'] == 'recheck':
                        db.update('findings', {'id': j['ref_id']}, state='unknown', reason='Re-check failed repeatedly', updated_at=iso(now()))
                        from . import cases
                        cases.ensure_case(db, j['ref_id'])
                else:
                    db.update('jobs', {'id': j['id']}, attempts=att, last_error=f'{type(e).__name__}: {e}'[:300], locked_until=None,
                              run_at=iso(now() + timedelta(minutes=min(2 ** att, 30))))
        n += 1
    db.run('INSERT INTO heartbeats(name, at, detail) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET at=excluded.at, detail=excluded.detail',
           'worker', iso(now()), str(n))
    return n


def loop(interval=3.0, stop: threading.Event | None = None):
    db = get_db()
    while not (stop and stop.is_set()):
        try:
            tick(db)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)


def start_thread() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=loop, kwargs={'stop': stop}, daemon=True, name='vaada-worker').start()
    return stop


if __name__ == '__main__':
    print('Vaada worker running', flush=True)
    loop()
