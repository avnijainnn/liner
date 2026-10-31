"""The manifest: Vaada's central object.

Lifecycle: draft (editable) -> sent for approval (token link) -> approved
(immutable version row + Ed25519 envelope + approval record). A draft never
becomes active silently; a version takes effect at approval and is closed by
the next approved version. Effective intervals are derived, never edited.
"""
from __future__ import annotations

import copy
import re
from datetime import timedelta

from . import crypto
from .db import Database, iso, now, parse_iso, dumps

ACTIONS = ['book', 'reschedule', 'cancel']
ASK_CLASSES = {
    'card_number': 'Card numbers / CVV',
    'password': 'Passwords',
    'otp': 'One-time / verification codes',
    'ssn': 'Social security numbers',
    'bank_account': 'Bank account / routing numbers',
    'date_of_birth': 'Date of birth',
    'insurance_id': 'Insurance member IDs',
}
PROMISE_CLASSES = {
    'price': 'Quoting prices',
    'discount': 'Discounts / waived fees',
    'refund': 'Refunds',
    'guarantee': 'Guarantees of outcome',
    'callback': 'Callbacks ("someone will call you back")',
    'sms_confirmation': 'Text confirmations',
    'email_confirmation': 'Email confirmations',
    'same_day': 'Same-day service',
    'staff_message': 'Passing a message to staff',
}
# Promise types that require the setup to actually deliver something.
DELIVERABLE_TYPES = ['callback', 'sms_confirmation', 'email_confirmation', 'same_day', 'staff_message']
DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

DEFAULT_POLICY = {
    'allowed_actions': ['book'],
    'deliverable_promises': ['sms_confirmation'],
    'never_promise': ['price', 'discount', 'refund', 'guarantee'],
    'never_ask': ['card_number', 'password', 'otp'],
    'handoff': {'on': 'caller_requests_person', 'to': 'front_desk', 'hours': 'business'},
    'business_hours': {d: (['08:00', '17:00'] if d not in ('sat', 'sun') else None) for d in DAYS},
    'check_timing': {'reads_min': [2, 15, 60]},
    'facts': {'prices': {}, 'refund_window_days': None, 'cancellation_notice_hours': None,
              'cancellation_fee': None, 'accepts_insurance': None},
    # Client business rules the agent's actions must respect (P0 #5)
    'booking_rules': {'within_business_hours': True, 'min_notice_hours': None, 'max_days_ahead': None, 'excluded_services': []},
    # Phrases that must always reach a person, at any hour
    'escalations': [],
    # A promised callback must exist as a task and be completed within this many hours
    'callback_sla_hours': 4,
}


class ManifestError(ValueError):
    pass


# ----------------------------------------------------------------- validation
def validate_policy(p: dict) -> dict:
    """Normalise and validate the editable policy. Raises ManifestError."""
    if not isinstance(p, dict):
        raise ManifestError('Policy must be an object')
    out = copy.deepcopy(DEFAULT_POLICY)
    for k in out:
        if k in p:
            out[k] = p[k]
    acts = list(dict.fromkeys(out['allowed_actions']))
    if any(a not in ACTIONS for a in acts):
        raise ManifestError(f'allowed_actions must be from {ACTIONS}')
    out['allowed_actions'] = sorted(acts, key=ACTIONS.index)
    for key, universe in (('never_ask', ASK_CLASSES), ('never_promise', PROMISE_CLASSES),
                          ('deliverable_promises', PROMISE_CLASSES)):
        vals = list(dict.fromkeys(out[key]))
        bad = [v for v in vals if v not in universe]
        if bad:
            raise ManifestError(f'{key}: unknown classes {bad}')
        out[key] = sorted(vals)
    clash = set(out['never_promise']) & set(out['deliverable_promises'])
    if clash:
        raise ManifestError(f'{sorted(clash)} cannot be both deliverable and never-promise')
    h = out['handoff'] or {}
    if h:
        if h.get('on') not in ('caller_requests_person', 'never', 'always_on_request'):
            raise ManifestError('handoff.on must be caller_requests_person, always_on_request or never')
        if h.get('hours') not in ('business', 'always'):
            raise ManifestError('handoff.hours must be business or always')
        out['handoff'] = {'on': h['on'], 'to': str(h.get('to') or 'front_desk')[:60], 'hours': h['hours']}
    bh = {}
    for d in DAYS:
        v = (out['business_hours'] or {}).get(d)
        if v:
            if not (isinstance(v, list) and len(v) == 2 and all(re.fullmatch(r'\d\d:\d\d', x) for x in v) and v[0] < v[1]):
                raise ManifestError(f'business_hours.{d} must be ["HH:MM","HH:MM"]')
        bh[d] = v or None
    out['business_hours'] = bh
    reads = [int(x) for x in (out['check_timing'] or {}).get('reads_min', [2, 15, 60])]
    if not reads or any(r < 1 or r > 1440 for r in reads):
        raise ManifestError('check_timing.reads_min must be minutes between 1 and 1440')
    out['check_timing'] = {'reads_min': sorted(set(reads))}
    br = dict(DEFAULT_POLICY['booking_rules'], **(out.get('booking_rules') or {}))
    try:
        for k in ('min_notice_hours', 'max_days_ahead'):
            br[k] = None if br.get(k) in (None, '') else int(float(br[k]))
            if br[k] is not None and br[k] < 0:
                raise ValueError
    except (TypeError, ValueError):
        raise ManifestError('booking_rules: notice and days ahead must be whole numbers')
    br['within_business_hours'] = bool(br.get('within_business_hours'))
    br['excluded_services'] = sorted({str(x).strip().lower() for x in (br.get('excluded_services') or []) if str(x).strip()})
    out['booking_rules'] = {k: br[k] for k in DEFAULT_POLICY['booking_rules']}
    esc = []
    for e in out.get('escalations') or []:
        trig = str((e or {}).get('trigger', '')).strip().lower()
        if trig:
            esc.append({'trigger': trig[:60], 'to': str(e.get('to') or 'front_desk')[:60]})
    out['escalations'] = sorted(esc, key=lambda e: e['trigger'])
    try:
        out['callback_sla_hours'] = max(1, int(float(out.get('callback_sla_hours') or 4)))
    except (TypeError, ValueError):
        raise ManifestError('callback_sla_hours must be a whole number')
    from .facts import validate_facts
    try:
        out['facts'] = validate_facts(out.get('facts'))
    except ValueError as e:
        raise ManifestError(f'facts: {e}')
    return out


def build_body(client: dict, policy: dict, version: int, prev_hash: str | None,
               approved_agent_version: str, agent_fingerprint: str | None) -> dict:
    """The exact object that gets hashed. Fields from the spec, nothing else."""
    return {
        'manifest_id': f"{client['agency_slug']}/{client['slug']}",
        'version': version,
        'prev_hash': prev_hash,
        'agent': {'platform': 'retell' if str(client['retell_agent_id']).startswith('agent_') else 'vapi', 'agent_id': client['retell_agent_id'],
                  'approved_agent_version': str(approved_agent_version),
                  'fingerprint': agent_fingerprint},
        'record': {'system': 'highlevel', 'location_id': client.get('hl_location_id'),
                   'calendars': [c['id'] for c in client.get('calendars', [])]},
        'timezone': client['timezone'],
        **policy,
    }


# ----------------------------------------------------------- agent fingerprint
def tool_capability(tool: dict) -> str:
    t = (tool.get('type') or '').lower()
    n = (tool.get('name') or '').lower()
    s = t + ' ' + n
    if 'transfer' in s:
        return 'handoff'
    if 'cancel' in s:
        return 'cancel'
    if 'reschedul' in s or 'move_appointment' in s or 'update_appointment' in s:
        return 'reschedule'
    if 'book' in s or 'create_appointment' in s or 'schedule' in s:
        return 'book'
    if 'availability' in s or 'check_' in s or 'get_' in s or 'lookup' in s:
        return 'read'
    if 'end_call' in s:
        return 'end_call'
    if 'sms' in s or 'text' in s:
        return 'sms_confirmation'
    if 'email' in s:
        return 'email_confirmation'
    return 'custom:' + (n or t or 'unknown')


def agent_fingerprint(snapshot: dict | None) -> str | None:
    """Hash of the agent's behavioural surface: normalised prompt + tool contract."""
    if not snapshot or ('prompt' not in snapshot and 'tools' not in snapshot):
        return None
    prompt = re.sub(r'\s+', ' ', (snapshot.get('prompt') or '')).strip().lower()
    tools = sorted(({'name': t.get('name'), 'type': t.get('type'),
                     'params': sorted((t.get('parameters') or {}).get('properties', {}).keys())}
                    for t in snapshot.get('tools', [])), key=lambda t: (t['type'] or '', t['name'] or ''))
    return crypto.sha256_hex(crypto.canonical({'prompt': prompt, 'tools': tools}))


def capabilities(snapshot: dict | None) -> set[str]:
    return {tool_capability(t) for t in (snapshot or {}).get('tools', [])}


# --------------------------------------------------------------- auto-draft
_PROMPT_HINTS = [
    (r'\b(text|sms)\b.{0,40}\bconfirm', 'sms_confirmation'),
    (r'\bemail\b.{0,40}\bconfirm', 'email_confirmation'),
    (r'\bcall (?:you|them) back\b|\bcallback\b', 'callback'),
    (r'\bpass (?:the|a|your) message\b|\bleave a message\b', 'staff_message'),
]


def auto_draft(client: dict, snapshot: dict | None) -> tuple[dict, list[str]]:
    """Pre-fill a policy from the Retell agent (prompt + tools) and HighLevel settings.

    Returns (policy, notes). Everything is a suggestion the agency confirms.
    """
    notes = []
    policy = copy.deepcopy(DEFAULT_POLICY)
    caps = capabilities(snapshot)
    acts = [a for a in ACTIONS if a in caps]
    if acts:
        policy['allowed_actions'] = acts
        notes.append(f"Allowed actions from the agent's tools: {', '.join(acts)}")
    else:
        notes.append('No booking tools found on the agent; defaulted to book. Confirm this.')
    prompt = (snapshot or {}).get('prompt', '') or ''
    deliver = set()
    for pat, cls in _PROMPT_HINTS:
        if re.search(pat, prompt, re.I):
            deliver.add(cls)
    if 'sms_confirmation' in caps:
        deliver.add('sms_confirmation')
    if deliver:
        policy['deliverable_promises'] = sorted(deliver)
        notes.append(f"Prompt mentions promises: {', '.join(sorted(deliver))}. Keep only those the setup can deliver.")
    for cls in list(policy['never_promise']):
        if cls in deliver:
            policy['never_promise'].remove(cls)
    if 'handoff' not in caps:
        policy['handoff'] = {'on': 'caller_requests_person', 'to': 'front_desk', 'hours': 'business'}
        notes.append('Agent has no transfer tool: any request for a person will be flagged as a missed handoff.')
    else:
        notes.append('Transfer tool found: handoff rule set to transfer during business hours.')
    m = re.search(r'(\d{1,2})(?::(\d\d))?\s*(am|a\.m\.)?\s*(?:-|to|until)\s*(\d{1,2})(?::(\d\d))?\s*(pm|p\.m\.)', prompt, re.I)
    if m:
        o = f"{int(m.group(1)):02d}:{m.group(2) or '00'}"
        c = f"{int(m.group(4)) % 12 + 12:02d}:{m.group(5) or '00'}"
        policy['business_hours'] = {d: ([o, c] if d not in ('sat', 'sun') else None) for d in DAYS}
        notes.append(f'Business hours read from prompt: {o}–{c} weekdays.')
    if not any((policy.get('facts') or {}).get(k) for k in ('prices', 'refund_window_days', 'cancellation_notice_hours')):
        notes.append('Add the business facts the agent may state (prices, refund window, cancellation notice). '
                     'Without them Vaada can flag forbidden promises but cannot tell a wrong answer from a right one.')
    return validate_policy(policy), notes


# ----------------------------------------------------------------- persistence
def client_ctx(db: Database, client_id: int) -> dict:
    c = db.one('SELECT c.*, a.slug AS agency_slug FROM clients c JOIN agencies a ON a.id=c.agency_id WHERE c.id=?', client_id)
    if not c:
        raise ManifestError('Client not found')
    c['calendars'] = c.j('calendars', [])
    c['slug'] = re.sub(r'[^a-z0-9]+', '-', (c['name'] or '').lower()).strip('-')
    c['agent_snapshot'] = c.j('agent_snapshot', None)
    return c


def versions(db: Database, client_id: int):
    return db.all('SELECT * FROM manifest_versions WHERE client_id=? ORDER BY version', client_id)


def latest(db: Database, client_id: int):
    return db.one('SELECT * FROM manifest_versions WHERE client_id=? ORDER BY version DESC LIMIT 1', client_id)


def intervals(db: Database, client_id: int) -> list[dict]:
    vs = versions(db, client_id)
    out = []
    for i, v in enumerate(vs):
        v['effective_to'] = vs[i + 1]['effective_from'] if i + 1 < len(vs) else None
        out.append(v)
    return out


def get_signing_key(db: Database, agency_id: int) -> dict:
    k = db.one('SELECT * FROM signing_keys WHERE agency_id=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1', agency_id)
    if k:
        return k
    pub, priv = crypto.new_keypair()
    slug = db.val('SELECT slug FROM agencies WHERE id=?', agency_id)
    n = (db.val('SELECT COUNT(*) FROM signing_keys WHERE agency_id=?', agency_id) or 0) + 1
    key_id = f'{slug}_k{n}'
    db.insert('signing_keys', agency_id=agency_id, key_id=key_id, public_key=pub,
              private_key_enc=crypto.encrypt(priv), created_at=iso(now()))
    return db.one('SELECT * FROM signing_keys WHERE key_id=?', key_id)


def create_draft(db: Database, client_id: int, policy: dict, *, by: str, source: str = 'manual') -> int:
    c = client_ctx(db, client_id)
    pol = validate_policy(policy)
    open_draft = db.one("SELECT id FROM manifest_drafts WHERE client_id=? AND state IN ('draft','sent')", client_id)
    if open_draft:
        raise ManifestError('This client already has an open draft. Edit or discard it first.')
    base = latest(db, client_id)
    t = iso(now())
    return db.insert('manifest_drafts', agency_id=c['agency_id'], client_id=client_id,
                     base_version=base['version'] if base else None, base_sha256=base['sha256'] if base else None,
                     policy=pol, source=source, state='draft', created_by=by, created_at=t, updated_at=t)


def update_draft(db: Database, draft_id: int, policy: dict):
    d = db.one('SELECT * FROM manifest_drafts WHERE id=?', draft_id)
    if not d or d['state'] not in ('draft', 'sent'):
        raise ManifestError('Only open drafts can be edited')
    # Editing invalidates any approval link already sent: the client must see what they approve.
    db.update('manifest_drafts', {'id': draft_id}, policy=validate_policy(policy), state='draft',
              approval_token_hash=None, approval_expires_at=None, updated_at=iso(now()))


def discard_draft(db: Database, draft_id: int):
    db.update('manifest_drafts', {'id': draft_id}, state='discarded', approval_token_hash=None, updated_at=iso(now()))


def pending_body(db: Database, draft: dict, approved_agent_version: str | None = None) -> dict:
    c = client_ctx(db, draft['client_id'])
    base = latest(db, draft['client_id'])
    snap = c['agent_snapshot'] or {}
    agent_ver = str(approved_agent_version or snap.get('version') or (base and base['approved_agent_version']) or '1')
    fp = agent_fingerprint(snap) if str(snap.get('version')) == agent_ver else None
    pol = draft.j('policy') if hasattr(draft, 'j') else draft['policy']
    return build_body(c, pol, (base['version'] + 1) if base else 1, base['sha256'] if base else None, agent_ver, fp)


def send_for_approval(db: Database, draft_id: int, *, to: str, days: int = 14) -> str:
    d = db.one('SELECT * FROM manifest_drafts WHERE id=?', draft_id)
    if not d or d['state'] not in ('draft', 'sent'):
        raise ManifestError('Draft is not open')
    tok = crypto.token()
    db.update('manifest_drafts', {'id': draft_id}, state='sent', approval_token_hash=crypto.token_hash(tok),
              approval_expires_at=iso(now() + timedelta(days=days)), sent_to=to, updated_at=iso(now()))
    return tok


def draft_by_token(db: Database, tok: str):
    d = db.one("SELECT * FROM manifest_drafts WHERE approval_token_hash=? AND state='sent'", crypto.token_hash(tok))
    if not d or parse_iso(d['approval_expires_at']) < now():
        return None
    return d


def approve(db: Database, draft_id: int, *, approver: str, role: str, method: str,
            expected_sha256: str | None = None, email: str | None = None, ip: str | None = None,
            approved_agent_version: str | None = None, effective_at=None) -> dict:
    """Turn a draft into an immutable, signed, approved version. Atomic."""
    if role not in ('client', 'agency'):
        raise ManifestError('role must be client or agency')
    if not approver or not approver.strip():
        raise ManifestError('A named approver is required')
    with db.tx():
        d = db.one('SELECT * FROM manifest_drafts WHERE id=?', draft_id)
        if not d or d['state'] not in ('draft', 'sent'):
            raise ManifestError('Draft is not open for approval')
        base = latest(db, d['client_id'])
        if (base['version'] if base else None) != d['base_version']:
            raise ManifestError(f"Draft was based on v{d['base_version']} but the current version is "
                                f"v{base['version'] if base else 'none'}. Re-create the draft.")
        body = pending_body(db, d, approved_agent_version)
        sha = crypto.hash_policy(body)
        if expected_sha256 and expected_sha256 != sha:
            raise ManifestError('The manifest changed after it was shown. Reload and review again.')
        t = now()
        if effective_at is not None:  # sandbox seeding only: real clients always take effect at approval time
            if not db.val('SELECT is_sandbox FROM clients WHERE id=?', d['client_id']):
                raise ManifestError('Effective time can only be set for sandbox clients')
            t = effective_at
        if base and parse_iso(base['effective_from']) >= t:
            t = parse_iso(base['effective_from']) + timedelta(microseconds=1)
        # Backdating guard: a new version can never take effect before a call already bound.
        last_bound = db.val('SELECT MAX(created_at) FROM binding_receipts WHERE client_id=?', d['client_id'])
        if last_bound and parse_iso(last_bound) >= t:
            t = parse_iso(last_bound) + timedelta(microseconds=1)
        vid = db.insert('manifest_versions', agency_id=d['agency_id'], client_id=d['client_id'],
                        version=body['version'], body=dumps(body), sha256=sha, prev_hash=body['prev_hash'],
                        approved_agent_version=body['agent']['approved_agent_version'],
                        agent_fingerprint=body['agent']['fingerprint'], effective_from=iso(t),
                        draft_id=draft_id, created_at=iso(now()))
        key = get_signing_key(db, d['agency_id'])
        sig = crypto.sign_hash(crypto.decrypt(key['private_key_enc']), sha)
        db.insert('signature_envelopes', agency_id=d['agency_id'], manifest_version_id=vid, manifest_sha256=sha,
                  alg='ed25519', signature=sig, key_id=key['key_id'], signed_at=iso(now()))
        db.insert('approvals', agency_id=d['agency_id'], manifest_version_id=vid, approved_sha256=sha,
                  approver_name=approver.strip()[:120], approver_email=(email or '')[:200] or None, role=role,
                  method=method, approved_at=iso(t), ip=ip)
        db.update('manifest_drafts', {'id': draft_id}, state='approved', approval_token_hash=None, updated_at=iso(now()))
        db.insert('audit_log', agency_id=d['agency_id'], actor=approver, action='manifest.approved', entity='manifest_version',
                  entity_id=str(vid), detail=dumps({'sha256': sha, 'role': role, 'method': method}), at=iso(now()))
    return db.one('SELECT * FROM manifest_versions WHERE id=?', vid)


def version_detail(db: Database, version_id: int) -> dict | None:
    v = db.one('SELECT * FROM manifest_versions WHERE id=?', version_id)
    if not v:
        return None
    v['body_obj'] = v.j('body')
    v['envelope'] = db.one('SELECT * FROM signature_envelopes WHERE manifest_version_id=?', version_id)
    v['approval'] = db.one('SELECT * FROM approvals WHERE manifest_version_id=?', version_id)
    nxt = db.one('SELECT effective_from FROM manifest_versions WHERE client_id=? AND version=?', v['client_id'], v['version'] + 1)
    v['effective_to'] = nxt['effective_from'] if nxt else None
    return v


def diff(a: dict | None, b: dict) -> list[dict]:
    """Field-level diff of two policy bodies (ignoring version/prev_hash)."""
    skip = {'version', 'prev_hash'}
    a = a or {}
    out = []
    for k in sorted(set(a) | set(b)):
        if k in skip or a.get(k) == b.get(k):
            continue
        av, bv = a.get(k), b.get(k)
        if isinstance(av, list) and isinstance(bv, list):
            out.append({'field': k, 'added': [x for x in bv if x not in av], 'removed': [x for x in av if x not in bv]})
        else:
            out.append({'field': k, 'from': av, 'to': bv})
    return out


def guardrails_prompt(body: dict) -> str:
    """v2 prevention step 3: the manifest as prompt guardrails the agency pastes in."""
    lines = [f"## Business rules (Vaada manifest v{body['version']}, {body.get('prev_hash') and 'chained' or 'first version'})",
             'You must follow these rules on every call. They were approved by the business.']
    acts = body.get('allowed_actions', [])
    lines.append(f"- You may only: {', '.join(acts) or 'answer questions'} appointments. Do not take any other action on the calendar.")
    if body.get('never_ask'):
        lines.append('- Never ask the caller for: ' + ', '.join(ASK_CLASSES[x].lower() for x in body['never_ask']) +
                     '. If the caller offers them, say you do not need them.')
    if body.get('never_promise'):
        lines.append('- Never promise or quote: ' + ', '.join(PROMISE_CLASSES[x].lower() for x in body['never_promise']) +
                     '. Say a team member will confirm instead, only if a callback is allowed below.')
    can = body.get('deliverable_promises', [])
    cannot = [x for x in DELIVERABLE_TYPES if x not in can]
    if can:
        lines.append('- You may promise: ' + ', '.join(PROMISE_CLASSES[x].lower() for x in can) + '.')
    if cannot:
        lines.append('- This business cannot deliver: ' + ', '.join(PROMISE_CLASSES[x].lower() for x in cannot) +
                     '. Never promise them.')
    h = body.get('handoff') or {}
    if h.get('on') in ('caller_requests_person', 'always_on_request'):
        when = 'during business hours' if h.get('hours') == 'business' else 'at any time'
        lines.append(f"- If the caller asks for a person, transfer them to {h.get('to', 'the front desk').replace('_', ' ')} {when}.")
    f = body.get('facts') or {}
    known = []
    for svc, price in (f.get('prices') or {}).items():
        known.append(f'{svc} costs ${price:g}')
    if f.get('refund_window_days') is not None:
        known.append(f"refunds can be requested within {f['refund_window_days']} days")
    if f.get('cancellation_notice_hours') is not None:
        known.append(f"cancellations need {f['cancellation_notice_hours']} hours notice")
    if f.get('cancellation_fee') is not None:
        known.append(f"the late-cancellation fee is ${f['cancellation_fee']}" if f['cancellation_fee'] else 'there is no cancellation fee')
    if f.get('accepts_insurance') is not None:
        known.append('we accept insurance' if f['accepts_insurance'] else 'we do not accept insurance')
    if known:
        lines.append('- Approved facts (state them exactly, never estimate): ' + '; '.join(known) + '.')
    br = body.get('booking_rules') or {}
    if br.get('within_business_hours'):
        lines.append('- Only book appointments inside business hours.')
    if br.get('min_notice_hours'):
        lines.append(f"- Never book less than {br['min_notice_hours']} hours ahead.")
    if br.get('max_days_ahead'):
        lines.append(f"- Never book more than {br['max_days_ahead']} days ahead.")
    if br.get('excluded_services'):
        lines.append('- Never book: ' + ', '.join(br['excluded_services']) + '. Offer a transfer or callback instead.')
    for e in body.get('escalations') or []:
        lines.append(f"- If the caller mentions \"{e['trigger']}\", transfer immediately to {e['to'].replace('_', ' ')}, at any hour.")
    lines.append('- If a transfer fails, take the caller\'s details and arrange a callback. Never just end the call.')
    lines.append('- Before ending the call, check you handled every request the caller made.')
    lines.append('- If a lookup fails or you are not sure of a fact, say a team member will confirm. Never guess.')
    lines.append('- When booking, read back the final day, date and time and wait for the caller to confirm before booking.')
    return '\n'.join(lines)
