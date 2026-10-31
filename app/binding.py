"""IWCB — Interval-Witnessed Capability Binding.

Vaada's binding algorithm decides which approved manifest version judges a call,
and makes that decision tamper-evident and re-derivable. Four parts:

1. Interval resolution (no fallback).  Approved versions form half-open
   intervals [from_i, from_{i+1}).  A call binds to the version whose interval
   contains its *start* time, found by binary search.  If no version was in
   force, the call is UNBOUND and its coverage is Unknown; Vaada never falls
   back to the latest version.

2. Boundary dual-judgment.  Call start times come from the voice platform's
   clock, approvals from Vaada's.  If a start lies within ±ε of a version
   boundary, the binding is BOUNDARY: the call is judged under both adjacent
   versions; a finding stands outright only if both versions agree, otherwise
   it is held for review with a boundary note.  (Clock skew can never silently
   decide which rules applied.)

3. Capability-surface drift.  Instead of only comparing version numbers, Vaada
   fingerprints the agent's behavioural surface (normalised prompt + tool
   contract) and maps tools to capabilities.  Drift is classified as
   renumbered (same surface), prompt_changed, capability_contraction,
   capability_expansion (the agent can now do something the business never
   approved) or version_mismatch (no snapshot available).  Severity follows
   the class, so a re-published identical agent doesn't page anyone while a
   newly added cancel tool does.

4. Witnessed receipt chain.  Each binding writes a receipt containing the
   chosen hash, its neighbours, the chain head at bind time and the drift
   class, hash-chained per client: r_n = H(r_{n-1} ‖ canonical(payload_n)).
   Because every receipt is *re-derivable* from the version table, the
   integrity check catches the attack plain hash chains miss: appending a
   correctly-chained new version with a back-dated effective time ("backdated
   append") would change the re-derived binding of an earlier call.  Daily
   Merkle roots over receipts let a report (and, in v3, a public log) commit to
   every binding of the day with one hash.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import crypto
from .db import Database, iso, now, parse_iso, dumps
from .manifest import agent_fingerprint, capabilities, intervals as manifest_intervals

SEVERITY = {'none': None, 'renumbered': 'low', 'prompt_changed': 'medium', 'version_mismatch': 'medium',
            'capability_contraction': 'medium', 'capability_expansion': 'high', 'fingerprint_mismatch': 'high'}


@dataclass
class Binding:
    status: str                       # bound | boundary | unbound | no_start_time
    primary: dict | None = None       # manifest_versions row
    candidates: list = field(default_factory=list)
    witness: dict = field(default_factory=dict)


def resolve(versions: list[dict], start: datetime | None, eps_s: int) -> Binding:
    """Pure function: versions are approved rows sorted by effective_from."""
    if start is None:
        return Binding('no_start_time')
    if not versions:
        return Binding('unbound', witness={'chain_length': 0})
    froms = [parse_iso(v['effective_from']) for v in versions]
    i = bisect.bisect_right(froms, start) - 1
    head = versions[-1]
    eps = timedelta(seconds=eps_s)
    near = set()
    if i >= 0:
        near.add(i)
        if i - 1 >= 0 and start - froms[i] < eps:
            near.add(i - 1)
    if i + 1 < len(versions) and froms[i + 1] - start < eps:
        near.add(i + 1)
        if i < 0:
            near.add(i + 1)
    if i < 0 and not near:
        return Binding('unbound', witness={'chain_length': len(versions), 'first_from': iso(froms[0]),
                                           'chain_head': head['sha256']})
    primary_idx = i if i >= 0 else min(near)
    cands = [versions[primary_idx]] + [versions[j] for j in sorted(near) if j != primary_idx]
    witness = {
        'prev': versions[primary_idx - 1]['sha256'] if primary_idx > 0 else None,
        'sha256': versions[primary_idx]['sha256'],
        'next': versions[primary_idx + 1]['sha256'] if primary_idx + 1 < len(versions) else None,
        'chain_head': head['sha256'],
        'chain_length': len(versions),
        'interval': [iso(froms[primary_idx]), iso(froms[primary_idx + 1]) if primary_idx + 1 < len(versions) else None],
    }
    status = 'boundary' if len(cands) > 1 else 'bound'
    if i < 0:
        # Start precedes the first approval but within ε of it: only a boundary candidate exists.
        status = 'boundary'
    return Binding(status, versions[primary_idx], cands, witness)


def approved_caps(body: dict) -> set[str]:
    caps = set(body.get('allowed_actions', [])) | {'read', 'end_call'}
    if (body.get('handoff') or {}).get('on') not in (None, 'never'):
        caps.add('handoff')
    caps |= set(body.get('deliverable_promises', []))
    return caps


def classify_drift(body: dict, running_version: str, running_snapshot: dict | None) -> tuple[str, dict]:
    approved_v = str(body['agent']['approved_agent_version'])
    approved_fp = body['agent'].get('fingerprint')
    run_fp = agent_fingerprint(running_snapshot) if running_snapshot else None
    if str(running_version) == approved_v:
        if approved_fp and run_fp and approved_fp != run_fp:
            return 'fingerprint_mismatch', {'approved_fp': approved_fp, 'running_fp': run_fp}
        return 'none', {}
    detail = {'approved_version': approved_v, 'running_version': str(running_version)}
    if not running_snapshot:
        return 'version_mismatch', detail
    if approved_fp and run_fp == approved_fp:
        return 'renumbered', detail
    run_caps = capabilities(running_snapshot)
    allowed = approved_caps(body)
    extra = sorted(c for c in run_caps if c not in allowed)
    if extra:
        return 'capability_expansion', {**detail, 'unapproved_capabilities': extra}
    lost = sorted(c for c in ({'handoff'} & allowed) | set(body.get('allowed_actions', [])) if c not in run_caps)
    if lost:
        return 'capability_contraction', {**detail, 'missing_capabilities': lost}
    return 'prompt_changed', detail


def _receipt_hash(prev: str | None, payload: dict) -> str:
    return crypto.sha256_hex((prev or 'genesis').encode() + b'|' + crypto.canonical(payload))


def bind_call(db: Database, call: dict, eps_s: int, running_snapshot: dict | None) -> tuple[Binding, dict, int]:
    """Bind a stored call. Returns (binding, receipt_payload, receipt_id). Call inside a tx."""
    versions = [v for v in manifest_intervals(db, call['client_id'])]
    start = parse_iso(call['started_at'])
    b = resolve(versions, start, eps_s)
    drift_class, drift = ('none', {})
    if b.primary:
        drift_class, drift = classify_drift(b.primary.j('body'), call['agent_version'], running_snapshot)
    last = db.one('SELECT seq, receipt_hash FROM binding_receipts WHERE client_id=? ORDER BY seq DESC LIMIT 1', call['client_id'])
    payload = {
        'alg': 'iwcb-1', 'call': call['retell_call_id'], 'client_id': call['client_id'],
        'start': call['started_at'], 'eps_s': eps_s, 'status': b.status,
        'manifest_version': b.primary['version'] if b.primary else None,
        'manifest_sha256': b.primary['sha256'] if b.primary else None,
        'candidates': [c['sha256'] for c in b.candidates], 'witness': b.witness,
        'agent_version': str(call['agent_version']), 'agent_fingerprint': call.get('agent_fingerprint'),
        'drift': drift_class, 'bound_at': iso(now()),
    }
    rh = _receipt_hash(last['receipt_hash'] if last else None, payload)
    rid = db.insert('binding_receipts', agency_id=call['agency_id'], client_id=call['client_id'], call_id=call['id'],
                    seq=(last['seq'] + 1) if last else 1, prev_receipt_hash=last['receipt_hash'] if last else None,
                    receipt_hash=rh, payload=dumps(payload), created_at=payload['bound_at'])
    payload['drift_detail'] = drift
    return b, payload, rid


def merkle_root(hashes: list[str]) -> str | None:
    if not hashes:
        return None
    level = [bytes.fromhex(h) for h in hashes]
    import hashlib
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(b'\x01' + level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return level[0].hex()


def daily_roots(db: Database, client_id: int) -> list[dict]:
    rows = db.all('SELECT receipt_hash, created_at FROM binding_receipts WHERE client_id=? ORDER BY seq', client_id)
    by_day: dict[str, list[str]] = {}
    for r in rows:
        by_day.setdefault(r['created_at'][:10], []).append(r['receipt_hash'])
    return [{'day': d, 'count': len(h), 'root': merkle_root(h)} for d, h in sorted(by_day.items(), reverse=True)]


def verify_integrity(db: Database, client_id: int) -> dict:
    """Walk the manifest chain and the receipt chain; re-derive every binding."""
    issues: list[str] = []
    vs = manifest_intervals(db, client_id)
    prev_sha, prev_from = None, None
    keys = {k['key_id']: k for k in db.all('SELECT * FROM signing_keys WHERE agency_id=(SELECT agency_id FROM clients WHERE id=?)', client_id)}
    for v in vs:
        body = v.j('body')
        sha = crypto.hash_policy(body)
        tag = f"v{v['version']}"
        if sha != v['sha256']:
            issues.append(f'{tag}: stored body does not hash to its recorded sha256')
        if body.get('prev_hash') != prev_sha or v['prev_hash'] != prev_sha:
            issues.append(f'{tag}: prev_hash does not link to the previous version (chain broken or version deleted)')
        if body.get('version') != v['version']:
            issues.append(f'{tag}: version number inside body differs from row')
        env = db.one('SELECT * FROM signature_envelopes WHERE manifest_version_id=?', v['id'])
        if not env:
            issues.append(f'{tag}: no signature envelope')
        else:
            k = keys.get(env['key_id'])
            if not k or not crypto.verify_hash(k['public_key'], v['sha256'], env['signature']):
                issues.append(f"{tag}: Ed25519 signature does not verify under key {env['key_id']}")
        ap = db.one('SELECT * FROM approvals WHERE manifest_version_id=?', v['id'])
        if not ap:
            issues.append(f'{tag}: no approval record')
        elif ap['approved_sha256'] != v['sha256']:
            issues.append(f'{tag}: approval is for a different hash than the stored version')
        f = parse_iso(v['effective_from'])
        if prev_from and f <= prev_from:
            issues.append(f'{tag}: effective_from is not after the previous version')
        prev_sha, prev_from = v['sha256'], f
    receipts = db.all('SELECT * FROM binding_receipts WHERE client_id=? ORDER BY seq', client_id)
    prev = None
    rederived_ok = 0
    for i, r in enumerate(receipts, 1):
        p = r.j('payload')
        if r['seq'] != i:
            issues.append(f"receipt #{r['seq']}: sequence gap (receipt deleted?)")
        if r['prev_receipt_hash'] != prev or _receipt_hash(prev, p) != r['receipt_hash']:
            issues.append(f"receipt #{r['seq']}: hash chain broken")
        bound_at = parse_iso(p['bound_at'])
        known = [v for v in vs if parse_iso(v['effective_from']) <= bound_at]  # versions that existed at bind time
        b = resolve(known, parse_iso(p['start']) if p.get('start') else None, p.get('eps_s', 90))
        sha = b.primary['sha256'] if b.primary else None
        if sha != p.get('manifest_sha256') or b.status != p.get('status'):
            issues.append(f"receipt #{r['seq']} (call {p.get('call')}): re-derived binding is "
                          f"{sha and sha[:10]} / {b.status}, receipt says {(p.get('manifest_sha256') or '')[:10]} / {p.get('status')} "
                          f"— a version was inserted or back-dated after this call was judged")
        else:
            rederived_ok += 1
        prev = r['receipt_hash']
    return {'ok': not issues, 'issues': issues, 'versions': len(vs), 'receipts': len(receipts),
            'rederived_ok': rederived_ok, 'head': vs[-1]['sha256'] if vs else None,
            'receipt_head': prev, 'roots': daily_roots(db, client_id)[:7]}
