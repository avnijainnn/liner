"""HTTP layer (Starlette). Server-rendered screens + JSON API + Retell webhook.

Security basics: session auth with scrypt passwords, CSRF token on every form,
tenant isolation (every query is scoped to the session's agency), signed Retell
webhooks verified on the raw body, secrets encrypted at rest, no write paths to
customer systems.
"""
from __future__ import annotations

import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import accounts, binding, cases, checks, config, crypto, manifest, pipeline, reports, trials, verify, worker
from .db import dumps, get_db, iso, loads, now, parse_iso
from .integrations import retell
from .integrations.highlevel import CRMError, crm_for
from .integrations import vapi

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / 'templates'))


# ------------------------------------------------------------------ filters
def ago(s):
    if not s:
        return '—'
    dt = parse_iso(s) if isinstance(s, str) else s
    d = now() - dt
    secs = d.total_seconds()
    fut = secs < 0
    secs = abs(secs)
    if secs < 60:
        txt = 'just now' if not fut else 'in under a minute'
        return txt
    for unit, n in (('d', 86400), ('h', 3600), ('m', 60)):
        if secs >= n:
            v = int(secs // n)
            return f'in {v}{unit}' if fut else f'{v}{unit} ago'


def localtime(s, tz='UTC', fmt='%a %d %b, %-I:%M %p'):
    if not s:
        return '—'
    dt = parse_iso(s) if isinstance(s, str) else s
    return dt.astimezone(ZoneInfo(tz or 'UTC')).strftime(fmt)


def short(h, n=10):
    return (h or '')[:n]


templates.env.filters.update(ago=ago, localtime=localtime, short=short, j=lambda s, d=None: loads(s, d) if isinstance(s, str) else (s or d),
                             title=lambda t: checks.TITLES.get(t, (t or '').replace('_', ' ').capitalize()),
                             pct=reports.pct, tojson_pretty=lambda v: json.dumps(v, indent=2, ensure_ascii=False))
def pill(s, label=None):
    from markupsafe import Markup, escape
    s = s or 'unknown'
    return Markup(f'<span class="pill s-{escape(s)}">{escape(label or str(s).replace("_", " "))}</span>')


def problem_tag(t):
    from markupsafe import Markup
    p = checks.PROBLEM.get(t)
    if not p:
        return ''
    label = {'semantic': 'What was said', 'outcome': 'What happened', 'execution': 'How it ran', 'deployment': 'Agent setup'}[p]
    return Markup(f'<span class="ptag p-{p}">{label}</span>')


def stage_strip(st):
    from markupsafe import Markup, escape
    if not st or not st.get('list'):
        return ''
    return Markup('<ol class="stages">' + ''.join(f'<li class="{escape(x["state"])}" title="{escape(x.get("note") or "")}">{escape(x["name"])}</li>'
                                                  for x in st['list']) + '</ol>')


def stage_card(st):
    from markupsafe import Markup, escape
    from .evidence import LABEL
    if not st or not st.get('list'):
        return ''
    rows = ''.join(f'<li class="{escape(x["state"])}"><span><b>{escape(x["name"])}</b><em>{LABEL.get(x["state"], x["state"])}</em>'
                   + (f'<small>{escape(x["note"])}</small>' if x.get('note') else '') + '</span></li>' for x in st['list'])
    ign = ''.join(f'<p><span class="mono">{escape(i.get("id") or "")}</span> “{escape(i.get("label") or "")}”: {escape(i.get("why") or "")}</p>' for i in st.get('ignored') or [])
    return Markup('<div class="card" style="margin-top:16px"><div class="row between"><h3>Completion stages</h3><span class="muted" style="font-size:13px">'
                  'Each stage needs its own evidence. A ticked box doesn’t count for the stages after it.</span></div>'
                  f'<ol class="stages big">{rows}</ol>' + (f'<div class="ignored"><b>Also in the CRM, not counted</b>{ign}</div>' if ign else '') + '</div>')


templates.env.globals.update(problem_tag=problem_tag, stage_strip=stage_strip, stage_card=stage_card, RECORD_TYPES=checks.RECORD_TYPES, pill=pill, ASK_CLASSES=manifest.ASK_CLASSES, PROMISE_CLASSES=manifest.PROMISE_CLASSES,
                             DELIVERABLE_TYPES=manifest.DELIVERABLE_TYPES, ACTIONS=manifest.ACTIONS, DAYS=manifest.DAYS,
                             DEMO_MODE=config.DEMO_MODE)


# --------------------------------------------------------------- auth/csrf
def user(request: Request):
    uid = request.session.get('uid')
    if not uid:
        return None
    db = get_db()
    u = db.one('SELECT u.*, a.name AS agency_name, a.slug AS agency_slug, a.is_sandbox FROM users u JOIN agencies a ON a.id=u.agency_id WHERE u.id=?', uid)
    return u


def need(request: Request, role='viewer'):
    u = user(request)
    if not u:
        raise HTTPException(303, headers={'Location': '/login?next=' + request.url.path})
    order = {'viewer': 0, 'operator': 1, 'owner': 2}
    if order[u['role']] < order[role]:
        raise HTTPException(403, 'Your role cannot do this')
    return u


def csrf_token(request: Request) -> str:
    t = request.session.get('csrf')
    if not t:
        t = request.session['csrf'] = secrets.token_urlsafe(24)
    return t


async def form(request: Request) -> dict:
    f = await request.form()
    data = {k: f.getlist(k) if len(f.getlist(k)) > 1 else f.get(k) for k in f.keys()}
    tok = data.pop('csrf', '')
    if not tok or not hmac.compare_digest(str(tok), request.session.get('csrf', '')):
        raise HTTPException(403, 'Form expired. Reload the page and try again.')
    return data


def render(request: Request, name: str, ctx: dict | None = None, status=200):
    ctx = dict(ctx or {})
    u = ctx.get('u') or user(request)
    ctx.update(request=request, u=u, csrf=csrf_token(request), flash=request.session.pop('flash', None),
               nav_counts=_nav_counts(u) if u else {})
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def flash(request, msg, kind='ok'):
    request.session['flash'] = {'msg': msg, 'kind': kind}


def back(url):
    return RedirectResponse(url, status_code=303)


def _nav_counts(u):
    db = get_db()
    return {'inbox': db.val("""SELECT COUNT(*) FROM findings f JOIN calls c ON c.id=f.call_id
                              LEFT JOIN cases k ON k.finding_id=f.id
                              WHERE f.agency_id=? AND c.source<>'trial' AND (
                                (f.type NOT IN ('booking','message_promised','callback_promised','contact_update') AND (f.state='open' OR (f.state='confirmed' AND k.status IN ('open','retesting')))) OR
                                (f.type IN ('booking','message_promised','callback_promised','contact_update') AND k.status IN ('open','fixed_awaiting_contact')))""", u['agency_id']) or 0}


def owned(table: str, id_: int, u) -> dict:
    row = get_db().one(f'SELECT * FROM {table} WHERE id=? AND agency_id=?', id_, u['agency_id'])
    if not row:
        raise HTTPException(404)
    return row


# ------------------------------------------------------------------- pages
async def login(request: Request):
    if request.method == 'POST':
        f = await form(request)
        u = accounts.authenticate(get_db(), f.get('email'), f.get('password'))
        if not u:
            return render(request, 'login.html', {'error': 'Email or password is wrong', 'email': f.get('email')}, 401)
        request.session.clear()
        request.session['uid'] = u['id']
        nxt = request.query_params.get('next', '/')
        return back(nxt if nxt.startswith('/') and not nxt.startswith('//') else '/')
    return render(request, 'login.html', {'demo': config.DEMO_MODE})


async def signup(request: Request):
    if request.method == 'POST':
        f = await form(request)
        try:
            aid = accounts.create_agency(get_db(), f.get('agency') or '', f.get('email') or '', f.get('name') or '', f.get('password') or '')
        except accounts.AccountError as e:
            return render(request, 'signup.html', {'error': str(e), 'f': f}, 400)
        u = get_db().one('SELECT id FROM users WHERE agency_id=?', aid)
        request.session.clear()
        request.session['uid'] = u['id']
        flash(request, 'Agency created. Add your first client to start.')
        return back('/clients')
    return render(request, 'signup.html', {'f': {}})


async def logout(request: Request):
    await form(request)
    request.session.clear()
    return back('/login')


async def demo_login(request: Request):
    if not config.DEMO_MODE:
        raise HTTPException(404)
    await form(request)
    from . import seed
    aid = seed.seed(get_db(), quiet=True)
    u = get_db().one("SELECT id FROM users WHERE agency_id=? AND role='owner'", aid)
    request.session.clear()
    request.session['uid'] = u['id']
    return back('/')


def _finding_rows(db, agency_id, where='', args=(), limit=200):
    return db.all(f"""SELECT f.*, c.retell_call_id, c.started_at, c.ended_at, c.source, cl.name AS client_name, cl.timezone,
                             k.id AS case_id, k.status AS case_status, k.owner, k.priority, k.due_at, k.customer_contact
                      FROM findings f JOIN calls c ON c.id=f.call_id JOIN clients cl ON cl.id=f.client_id
                      LEFT JOIN cases k ON k.finding_id=f.id
                      WHERE f.agency_id=? AND c.source<>'trial' {where}
                      ORDER BY CASE f.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                               COALESCE(f.appointment_at, '9999') ASC, f.id DESC LIMIT {int(limit)}""", agency_id, *args)


async def inbox(request: Request):
    u = need(request)
    db = get_db()
    rows = _finding_rows(db, u['agency_id'], """AND ((f.type NOT IN ('booking','message_promised','callback_promised','contact_update') AND (f.state='open' OR (f.state='confirmed' AND k.status IN ('open','retesting')))) OR
                          (f.type IN ('booking','message_promised','callback_promised','contact_update') AND k.status IN ('open','fixed_awaiting_contact')))""")
    for r in rows:
        if r['type'] in ('booking', 'callback_promised', 'message_promised'):
            o = db.one('SELECT fields, reason FROM observations WHERE finding_id=? ORDER BY id DESC LIMIT 1', r['id'])
            if r['type'] != 'booking':
                r['stages'] = _stages(db, r, o)
        if r['type'] == 'booking':
            recs = loads(o['fields'], {}).get('records', []) if o else []
            pid = r.j('promised', {}).get('appointment_id')
            rec = next((x for x in recs if x.get('id') == pid), recs[0] if recs else None)
            k = db.one('SELECT * FROM cases WHERE finding_id=?', r['id'])
            r['stages'] = _stages(db, r, o, rec, k)
            r['cal'] = (localtime(rec['start_at'], r['timezone'], '%a %d %b, %-I:%M %p') + f" · {rec.get('status')}") if rec and rec.get('start_at') else (o['reason'] if o else 'Not read yet')
    watching = _finding_rows(db, u['agency_id'], "AND f.type IN ('booking','message_promised','callback_promised','contact_update') AND f.state='pending'", limit=10)
    m = reports.agency_metrics(db, u['agency_id'])
    clients = db.all('SELECT * FROM clients WHERE agency_id=?', u['agency_id'])
    hour = now().astimezone(ZoneInfo('Asia/Kolkata' if False else 'UTC')).hour
    groups = {}
    for r in rows:
        key = (r['type'], str((r.j('rule', {}) or {}).get('value')))
        groups.setdefault(key, set()).add(r['client_name'])
    patterns = sorted([{'type': k[0], 'value': k[1], 'clients': sorted(v)} for k, v in groups.items() if len(v) >= 2],
                      key=lambda x: -len(x['clients']))
    prob_counts = {}
    for r in rows:
        k = checks.PROBLEM.get(r['type'])
        prob_counts[k] = prob_counts.get(k, 0) + 1
    return render(request, 'inbox.html', {'rows': rows, 'watching': watching, 'm': m, 'clients': clients, 'u': u, 'prob_counts': prob_counts, 'patterns': patterns})


async def findings_queue(request: Request):
    u = need(request)
    db = get_db()
    q = request.query_params
    where, args = [], []
    if q.get('client'):
        where.append('AND f.client_id=?'); args.append(int(q['client']))
    if q.get('type'):
        where.append('AND f.type=?'); args.append(q['type'])
    if q.get('state'):
        where.append('AND f.state=?'); args.append(q['state'])
    if q.get('owner'):
        where.append('AND k.owner=?'); args.append(q['owner'])
    if q.get('problem'):
        types = [t for t, p in checks.PROBLEM.items() if p == q['problem']] or ['none']
        where.append(f"AND f.type IN ({','.join('?' * len(types))})"); args.extend(types)
    rows = _finding_rows(db, u['agency_id'], ' '.join(where), args, limit=500)
    clients = db.all('SELECT id, name FROM clients WHERE agency_id=? ORDER BY name', u['agency_id'])
    owners = [r['owner'] for r in db.all('SELECT DISTINCT owner FROM cases WHERE agency_id=? AND owner IS NOT NULL', u['agency_id'])]
    return render(request, 'findings.html', {'rows': rows, 'clients': clients, 'owners': owners, 'q': dict(q)})


def _stages(db, f, o, rec=None, case=None):
    """Completion stages for a record promise: from the latest check, or derived for bookings."""
    from . import evidence
    fields = loads(o['fields'], {}) if o else {}
    if f['type'] in ('callback_promised', 'message_promised'):
        return {'list': fields.get('stages') or [], 'ignored': fields.get('ignored') or []}
    if f['type'] == 'booking':
        sms = db.one("SELECT state FROM findings WHERE call_id=? AND type='message_promised'", f['call_id'])
        return {'list': evidence.booking_stages(f, rec, bool(o), dict(case) if case else None, sms['state'] if sms else None), 'ignored': []}
    return None


def _finding_ctx(db, u, fid):
    f = owned('findings', fid, u)
    call = db.one('SELECT * FROM calls WHERE id=?', f['call_id'])
    client = db.one('SELECT * FROM clients WHERE id=?', f['client_id'])
    obs = db.all('SELECT * FROM observations WHERE finding_id=? ORDER BY id DESC', fid)
    case = db.one('SELECT * FROM cases WHERE finding_id=?', fid)
    events = db.all('SELECT * FROM case_events WHERE case_id=? ORDER BY id', case['id']) if case else []
    st = db.one('SELECT * FROM call_state WHERE call_id=?', call['id'])
    receipt = db.one('SELECT * FROM binding_receipts WHERE call_id=?', call['id'])
    mv = db.one('SELECT * FROM manifest_versions WHERE client_id=? AND sha256=?', client['id'], f['manifest_sha256']) if f['manifest_sha256'] else None
    jobs = db.all("SELECT * FROM jobs WHERE kind='recheck' AND ref_id=? ORDER BY run_at", fid)
    users = db.all('SELECT name FROM users WHERE agency_id=?', u['agency_id'])
    latest_rec = None
    if obs:
        recs = loads(obs[0]['fields'], {}).get('records') or []
        p = f.j('promised', {})
        latest_rec = next((r for r in recs if r.get('id') == p.get('appointment_id')), recs[0] if recs else None)
    from . import ledger as _ledger
    return dict(ledger=_ledger.entries(db, fid), linked=db.all("SELECT id, type, state FROM findings WHERE dedupe_key LIKE ? OR id=?", f"exec:{fid}:%", (loads(f['promised'], {}) or {}).get('commitment_id') or -1),
                f=f, call=call, client=client, obs=obs, case=case, events=events, st=st, receipt=receipt, mv=mv, jobs=jobs,
                turns=loads(call['turns'], []), tools=loads(call['tool_calls'], []), users=users, rec=latest_rec,
                approval=db.one('SELECT * FROM approvals WHERE manifest_version_id=?', mv['id']) if mv else None,
                stages=_stages(db, f, obs[0] if obs else None, latest_rec, case))


def next_step(ctx):
    f, case = ctx['f'], ctx['case']
    t = f['type']
    if t == 'booking':
        if f['state'] == 'contradicted' and 'answer one request' in (f['reason'] or ''):
            return 'Cancel the extra appointment in HighLevel (keep the one the caller agreed to), then press Re-check.'
        if f['state'] == 'contradicted':
            return 'Fix the appointment in HighLevel (or call the customer), then press Re-check.'
        if f['state'] == 'unknown':
            return 'Look up the caller in HighLevel. If the booking is right, Re-check; if it can\'t be checked, waive with a reason.'
        if f['state'] == 'pending':
            return 'Nothing yet: Vaada re-reads the calendar on schedule.'
        if case and case['status'] == 'fixed_awaiting_contact':
            return 'Record is fixed. Let the customer know, then mark them contacted.'
        return 'Nothing to do.'
    if t == 'callback_promised':
        return {'contradicted': 'Create or complete the callback task (and call the customer), then press Re-check.',
                'pending': 'Vaada is watching for the callback task to be created and completed.'}.get(f['state'], 'Nothing to do.')
    if t == 'contact_update':
        return {'contradicted': 'Update the contact in HighLevel, then press Re-check.',
                'pending': 'Vaada re-reads the contact on schedule.'}.get(f['state'], 'Nothing to do.')
    if t == 'message_promised':
        return {'contradicted': 'Send the caller the confirmation text (or call them), then press Re-check.',
                'pending': 'Nothing yet: Vaada re-reads the conversation on schedule.'}.get(f['state'], 'Nothing to do.')
    if t == 'wrong_information':
        return 'If the agent was wrong, confirm and correct the prompt or knowledge base; if the fact changed, update the manifest.'
    if t == 'ungrounded_answer':
        return 'Fix the failing lookup, and add the fact to the manifest so the agent stops guessing.'
    if t == 'duplicate_effect':
        return 'Cancel the extra appointment or apologise for the extra text, then confirm. Check why the agent wrote twice.'
    if t == 'retry_risk':
        return 'Make sure nothing retries this booking. Then fix the tool so a timeout looks up the request before writing again.'
    if t == 'agent_drift':
        return 'Re-approve the manifest for the running agent version, or roll the agent back.'
    if f['state'] == 'open':
        return 'Read the clip. Confirm if the agent got it wrong (this opens a case); dismiss with a reason if not.'
    if f['state'] == 'confirmed':
        return ('Fix the cause (prompt, tool or manifest), then press Verify fix. Vaada re-checks independently and '
                'closes the case only if the check passes.')
    return 'Reviewed.'


async def finding_detail(request: Request):
    u = need(request)
    ctx = _finding_ctx(get_db(), u, int(request.path_params['id']))
    ctx['next_step'] = next_step(ctx)
    return render(request, 'finding.html', ctx)


async def finding_review(request: Request):
    u = need(request, 'operator')
    fid = int(request.path_params['id'])
    owned('findings', fid, u)
    f = await form(request)
    db = get_db()
    try:
        with db.tx():
            cases.review_finding(db, fid, f.get('decision', ''), u['name'], f.get('reason', ''))
        flash(request, {'confirm': 'Confirmed. It will appear in the client report.', 'dismiss': 'Dismissed. The evidence and your reason stay on record.',
                        'reopen': 'Reopened.'}.get(f.get('decision'), 'Saved'))
    except cases.CaseError as e:
        flash(request, str(e), 'err')
    return back(f'/findings/{fid}')


async def case_act(request: Request):
    u = need(request, 'operator')
    cid = int(request.path_params['id'])
    c = owned('cases', cid, u)
    f = await form(request)
    db = get_db()
    try:
        with db.tx():
            out = cases.act(db, cid, f.get('action', ''), u['name'], owner=f.get('owner', ''), note=f.get('note', ''))
        fr = db.one('SELECT state FROM findings WHERE id=?', c['finding_id'])
        if f.get('action') == 'recheck':
            flash(request, 'Verified — the calendar now matches what the caller agreed.' if fr['state'] == 'verified'
                  else f"Still {fr['state']}: the calendar does not match yet.", 'ok' if fr['state'] == 'verified' else 'warn')
        else:
            flash(request, 'Saved to the case log.')
    except (cases.CaseError, ValueError) as e:
        flash(request, str(e), 'err')
    return back(f.get('return') or f"/findings/{c['finding_id']}")


async def call_detail(request: Request):
    u = need(request)
    db = get_db()
    call = owned('calls', int(request.path_params['id']), u)
    client = db.one('SELECT * FROM clients WHERE id=?', call['client_id'])
    st = db.one('SELECT * FROM call_state WHERE call_id=?', call['id'])
    receipt = db.one('SELECT * FROM binding_receipts WHERE call_id=?', call['id'])
    fs = db.all('SELECT * FROM findings WHERE call_id=?', call['id'])
    return render(request, 'call.html', {'call': call, 'client': client, 'st': st, 'receipt': receipt, 'fs': fs,
                                         'turns': loads(call['turns'], []), 'tools': loads(call['tool_calls'], [])})


# ----------------------------------------------------------------- clients
def _client_row(db, c):
    mv = db.one('SELECT * FROM manifest_versions WHERE client_id=? AND effective_from<=? ORDER BY version DESC LIMIT 1', c['id'], iso(now()))
    snap = loads(c['agent_snapshot'], {}) or {}
    last = db.one('SELECT c.received_at, s.drift_class, c.agent_version FROM calls c JOIN call_state s ON s.call_id=c.id '
                  "WHERE c.client_id=? AND c.source<>'trial' ORDER BY c.id DESC LIMIT 1", c['id'])
    since = iso(now() - timedelta(days=30))
    tot = db.val("SELECT COUNT(*) FROM calls c JOIN call_state s ON s.call_id=c.id WHERE c.client_id=? AND c.source<>'trial' AND c.received_at>=?", c['id'], since) or 0
    chk = db.val("SELECT COUNT(*) FROM calls c JOIN call_state s ON s.call_id=c.id WHERE c.client_id=? AND c.source<>'trial' AND c.received_at>=? "
                 "AND s.coverage NOT IN ('no_transcript','unknown_manifest') AND s.coverage IS NOT NULL", c['id'], since) or 0
    open_f = db.val("""SELECT COUNT(*) FROM findings f JOIN calls x ON x.id=f.call_id LEFT JOIN cases k ON k.finding_id=f.id
                       WHERE f.client_id=? AND x.source<>'trial' AND ((f.type NOT IN ('booking','message_promised','callback_promised','contact_update') AND f.state IN ('open','confirmed') AND (k.status IS NULL OR k.status IN ('open','retesting'))) OR k.status IN ('open','fixed_awaiting_contact'))""", c['id']) or 0
    draft = db.one("SELECT * FROM manifest_drafts WHERE client_id=? AND state IN ('draft','sent')", c['id'])
    running = (last and last['agent_version']) or snap.get('version')
    return dict(c=c, mv=mv, running=running, last=last, coverage=(chk, tot), open=open_f, draft=draft,
                drift=bool(mv and running and str(running) != str(mv['approved_agent_version'])))


async def clients_page(request: Request):
    u = need(request)
    db = get_db()
    if request.method == 'POST':
        need(request, 'owner')
        f = await form(request)
        try:
            cals = [{'id': x.strip(), 'name': x.strip()} for x in (f.get('calendars') or '').split(',') if x.strip()]
            cid = accounts.create_client(db, u['agency_id'], name=f.get('name', ''), retell_agent_id=f.get('agent_id', ''),
                                         timezone=f.get('timezone', 'America/Chicago'), location_label=f.get('location_label', ''),
                                         hl_location_id=f.get('hl_location_id', ''), calendars=cals, hl_token=f.get('hl_token') or None)
            flash(request, 'Client added. Next: sync the agent and draft the manifest.')
            return back(f'/clients/{cid}')
        except accounts.AccountError as e:
            flash(request, str(e), 'err')
            return back('/clients')
    rows = [_client_row(db, c) for c in db.all('SELECT * FROM clients WHERE agency_id=? ORDER BY name', u['agency_id'])]
    return render(request, 'clients.html', {'rows': rows})


async def client_detail(request: Request):
    u = need(request)
    db = get_db()
    c = owned('clients', int(request.path_params['id']), u)
    row = _client_row(db, c)
    calls = db.all("""SELECT c.*, s.coverage, s.binding_status, s.drift_class, s.manifest_sha256 AS bound_sha,
                      (SELECT COUNT(*) FROM findings f WHERE f.call_id=c.id AND f.type NOT IN ('booking','message_promised','callback_promised','contact_update') AND f.state NOT IN ('superseded','resolved','dismissed')) AS nflags,
                      (SELECT state FROM findings f WHERE f.call_id=c.id AND f.type='booking') AS booking_state
                      FROM calls c JOIN call_state s ON s.call_id=c.id WHERE c.client_id=? AND c.source<>'trial'
                      ORDER BY c.id DESC LIMIT 25""", c['id'])
    return render(request, 'client.html', {'row': row, 'c': c, 'calls': calls, 'snap': loads(c['agent_snapshot'], {}) or {},
                                           'scenarios': SIM_SCENARIOS})


SIM_SCENARIOS = [
    ('book:correction', 'Booking with a correction ("not 9, make it 10")'), ('book:simple', 'Simple booking'),
    ('book:relative', 'Booking for "next Friday"'), ('reschedule', 'Reschedule'), ('cancel', 'Cancel'),
    ('never_promise:discount', 'Caller pushes for a discount'), ('undeliverable:callback', 'Caller asks for a callback'),
    ('never_ask:card_number', 'Caller offers a card number'), ('handoff', 'Caller asks for a person'),
    ('fact_wrong:prices.cleaning', 'Agent quotes a wrong price'), ('fact_kb:refund_window_days', 'Lookup fails, agent guesses the refund policy'),
    ('exec:retry_dup', 'Booking tool times out, agent retries (double booking)'), ('exec:timeout_landed', 'Tool reports failure but the booking landed'),
    ('exec:dup_contact', 'Second booking under a duplicate contact'), ('exec:double_text', 'Confirmation text sent twice'),
    ('exec:no_text', 'Promised text never sent'),
]


async def client_simulate(request: Request):
    """Sandbox only: generate a call with the sandbox agent and push it through the signed webhook path."""
    u = need(request, 'operator')
    c = owned('clients', int(request.path_params['id']), u)
    if not c['is_sandbox']:
        raise HTTPException(400, 'Simulation is only for sandbox clients')
    f = await form(request)
    kind = f.get('scenario', 'book:simple')
    k, _, v = kind.partition(':')
    sc = {'kind': k}
    over = {}
    db = get_db()
    if k == 'book':
        sc['variant'] = v or 'simple'
    elif k in ('fact_wrong', 'fact_kb'):
        mv = manifest.latest(db, c['id'])
        fx = (mv.j('body') if mv else {}).get('facts') or {}
        val = (fx.get('prices') or {}).get(v[7:]) if v.startswith('prices.') else fx.get(v)
        if val is None:
            flash(request, 'Add that fact to the manifest first, then simulate.', 'warn')
            return back(f"/clients/{c['id']}/manifest")
        sc = {'kind': 'fact', 'rule': v, 'value': val}
        over = {'wrong_fact': 1.0} if k == 'fact_wrong' else {'kb_fail': 1.0}
    elif k == 'exec':
        sc = {'kind': 'book', 'variant': 'simple'}
        over = {v: 1.0, 'ignores_correction': 0, 'texts': True}
    elif v:
        sc['rule'] = v
    from .sim import SimAgent
    agent = SimAgent(db, c, f'ui:{secrets.token_hex(4)}')
    agent.p.update(over)
    payload = agent.run(sc, now() - timedelta(minutes=3))
    body = json.dumps(payload).encode()
    agency = db.one('SELECT * FROM agencies WHERE id=?', u['agency_id'])
    key = crypto.decrypt(agency['retell_api_key_enc'])
    res = await _handle_webhook(agency, body, retell.sign(body, key))
    if res.get('call_id'):
        pipeline.process_call(db, res['call_id'])
        if k in ('exec',):
            for fr in db.all("SELECT id FROM findings WHERE call_id=? AND type IN ('booking','message_promised') AND state='pending'", res['call_id']):
                with db.tx():
                    verify.check(db, fr['id'], 'final')
        flash(request, 'Sandbox call ingested through the signed webhook and checked. Calendar reads are scheduled at 2, 15 and 60 minutes.')
        return back(f"/calls/{res['call_id']}")
    flash(request, f'Webhook result: {res}', 'warn')
    return back(f"/clients/{c['id']}")


def _confirmation_body(db, client, finding_id) -> str:
    """The text the operator sends: the business name and the booked time, nothing else."""
    from zoneinfo import ZoneInfo
    f = db.one('SELECT call_id FROM findings WHERE id=?', int(finding_id)) if finding_id else None
    b = db.one("SELECT promised FROM findings WHERE call_id=? AND type='booking' ORDER BY id DESC", f['call_id']) if f else None
    st = (loads(b['promised'], {}) or {}).get('start_at') if b else None
    if not st:
        return f"{client['name']}: your appointment is confirmed."
    d = parse_iso(st).astimezone(ZoneInfo(client['timezone']))
    return f"{client['name']}: your appointment is confirmed for {d:%a %b} {d.day} at {d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}."


async def client_sync(request: Request):
    """Read the agent's prompt + tools from Retell and the location's calendars from HighLevel."""
    u = need(request, 'operator')
    c = owned('clients', int(request.path_params['id']), u)
    await form(request)
    db = get_db()
    if c['is_sandbox']:
        flash(request, 'Sandbox clients use the built-in agent snapshot.', 'warn')
        return back(f"/clients/{c['id']}")
    agency = db.one('SELECT * FROM agencies WHERE id=?', u['agency_id'])
    msgs = []
    try:
        api, platform = pipeline.agent_api(db, c)
        snap = api.agent_snapshot(c['retell_agent_id'])
        old = loads(c['agent_snapshot'], {}) or {}
        hist = old.get('history') or {}
        hist[str(snap['version'])] = snap
        snap['history'] = hist
        snap['trial_line'] = old.get('trial_line')
        db.update('clients', {'id': c['id']}, agent_snapshot=dumps(snap))
        msgs.append(f"Agent v{snap['version']} read from {platform} ({len(snap['tools'])} tools)")
    except Exception as e:
        msgs.append(f'Agent: {e}')
    try:
        crm = crm_for(db, c)
        info = crm.location_settings()
        upd = {'connection_status': 'connected', 'connection_checked_at': iso(now()), 'connection_error': None}
        if info.get('calendars'):
            upd['calendars'] = dumps(info['calendars'])
        db.update('clients', {'id': c['id']}, **upd)
        msgs.append(f"HighLevel: {len(info.get('calendars') or [])} calendars, timezone {info.get('timezone')}")
    except CRMError as e:
        db.update('clients', {'id': c['id']}, connection_status='error', connection_error=str(e)[:200], connection_checked_at=iso(now()))
        msgs.append(f'HighLevel: {e}')
    flash(request, ' · '.join(msgs), 'ok' if all(':' not in m or 'HighLevel: ' in m and 'HTTP' not in m for m in msgs) else 'warn')
    return back(f"/clients/{c['id']}")


# ---------------------------------------------------------------- manifest
async def manifest_page(request: Request):
    u = need(request)
    db = get_db()
    c = owned('clients', int(request.path_params['id']), u)
    vs = manifest.intervals(db, c['id'])
    for v in vs:
        v['approval'] = db.one('SELECT * FROM approvals WHERE manifest_version_id=?', v['id'])
        v['envelope'] = db.one('SELECT * FROM signature_envelopes WHERE manifest_version_id=?', v['id'])
        v['calls'] = db.val("SELECT COUNT(*) FROM call_state s JOIN calls c ON c.id=s.call_id WHERE s.manifest_version_id=? AND c.source<>'trial'", v['id'])
    current = next((v for v in reversed(vs) if parse_iso(v['effective_from']) <= now()), None)
    draft = db.one("SELECT * FROM manifest_drafts WHERE client_id=? AND state IN ('draft','sent') ORDER BY id DESC LIMIT 1", c['id'])
    pending = None
    if draft:
        pending = manifest.pending_body(db, draft)
        pending_sha = crypto.hash_policy(pending)
    snap = loads(c['agent_snapshot'], {}) or {}
    notes = []
    if not draft:
        _, notes = manifest.auto_draft(manifest.client_ctx(db, c['id']), snap)
    link = request.session.pop('approval_link', None)
    body = current.j('body') if current else None
    return render(request, 'manifest.html', {
        'c': c, 'vs': list(reversed(vs)), 'current': current, 'body': body, 'draft': draft,
        'dpol': draft.j('policy') if draft else None, 'pending': pending, 'pending_sha': pending_sha if draft else None,
        'diff': manifest.diff(body, pending) if draft else [], 'snap': snap, 'notes': notes, 'link': link,
        'guardrails': manifest.guardrails_prompt(body) if body else None,
        'trials': db.all('SELECT * FROM trials WHERE client_id=? ORDER BY id DESC LIMIT 5', c['id'])})


def _policy_from_form(f) -> dict:
    def lst(k):
        v = f.get(k)
        return v if isinstance(v, list) else ([v] if v else [])
    bh = {}
    for d in manifest.DAYS:
        if f.get(f'open_{d}'):
            bh[d] = [f.get(f'from_{d}') or '08:00', f.get(f'to_{d}') or '17:00']
        else:
            bh[d] = None
    reads = [int(x) for x in (f.get('reads') or '2,15,60').replace(' ', '').split(',') if x.strip().isdigit()]
    return {'allowed_actions': lst('allowed_actions'), 'deliverable_promises': lst('deliverable_promises'),
            'never_promise': lst('never_promise'), 'never_ask': lst('never_ask'),
            'handoff': {'on': f.get('handoff_on', 'caller_requests_person'), 'to': f.get('handoff_to') or 'front_desk',
                        'hours': f.get('handoff_hours', 'business')},
            'business_hours': bh, 'check_timing': {'reads_min': reads or [2, 15, 60]},
            'booking_rules': {'within_business_hours': bool(f.get('within_business_hours')), 'min_notice_hours': f.get('min_notice_hours') or None,
                              'max_days_ahead': f.get('max_days_ahead') or None,
                              'excluded_services': [x.strip() for x in (f.get('excluded_services') or '').split(',') if x.strip()]},
            'escalations': [{'trigger': a.strip(), 'to': (b.strip() or 'front_desk')} for a, _, b in
                            (ln.partition('=>') for ln in (f.get('escalations') or '').splitlines()) if a.strip()],
            'callback_sla_hours': f.get('callback_sla_hours') or 4,
            'facts': {'prices': {k.strip(): v.strip().lstrip('$') for k, _, v in (ln.partition('=') for ln in (f.get('prices') or '').splitlines()) if k.strip() and v.strip()},
                      'refund_window_days': f.get('refund_window_days') or None, 'cancellation_notice_hours': f.get('cancellation_notice_hours') or None,
                      'cancellation_fee': f.get('cancellation_fee') or None, 'accepts_insurance': f.get('accepts_insurance') or None}}


async def draft_create(request: Request):
    u = need(request, 'operator')
    db = get_db()
    c = owned('clients', int(request.path_params['id']), u)
    f = await form(request)
    try:
        if f.get('source') == 'copy':
            cur = manifest.latest(db, c['id'])
            if not cur:
                raise manifest.ManifestError('No approved version to copy')
            body = cur.j('body')
            pol = {k: body[k] for k in manifest.DEFAULT_POLICY}
            src = f"copy of v{cur['version']}"
        else:
            pol, _ = manifest.auto_draft(manifest.client_ctx(db, c['id']), loads(c['agent_snapshot'], {}))
            src = 'auto-draft from Retell + HighLevel'
        with db.tx():
            manifest.create_draft(db, c['id'], pol, by=u['name'], source=src)
        flash(request, 'Draft created. It is not active until it is approved.')
    except manifest.ManifestError as e:
        flash(request, str(e), 'err')
    return back(f"/clients/{c['id']}/manifest#draft")


async def draft_action(request: Request):
    u = need(request, 'operator')
    db = get_db()
    d = owned('manifest_drafts', int(request.path_params['id']), u)
    f = await form(request)
    act = request.path_params['action']
    to = f"/clients/{d['client_id']}/manifest#draft"
    try:
        with db.tx():
            if act == 'save':
                manifest.update_draft(db, d['id'], _policy_from_form(f))
                flash(request, 'Draft saved. Any approval link sent earlier is now void.')
            elif act == 'send':
                tok = manifest.send_for_approval(db, d['id'], to=f.get('to') or 'client')
                request.session['approval_link'] = f"{config.PUBLIC_URL}/approve/{tok}"
                flash(request, 'Approval link created. Send it to the business owner.')
            elif act == 'approve':
                if f.get('confirm') != 'yes':
                    raise manifest.ManifestError('Tick the box to confirm you are approving on the client\'s behalf')
                v = manifest.approve(db, d['id'], approver=f.get('approver') or u['name'], role='agency', method='agency_on_behalf',
                                     expected_sha256=f.get('sha'), email=u['email'], ip=request.client.host if request.client else None,
                                     approved_agent_version=f.get('agent_version') or None)
                flash(request, f"v{v['version']} approved on the client's behalf and signed. Reports will say the agency approved it.")
            elif act == 'discard':
                manifest.discard_draft(db, d['id'])
                flash(request, 'Draft discarded.')
    except manifest.ManifestError as e:
        flash(request, str(e), 'err')
    return back(to)


async def approve_public(request: Request):
    """Client approval link: no login, token-scoped to one draft."""
    db = get_db()
    tok = request.path_params['token']
    d = manifest.draft_by_token(db, tok)
    if not d:
        return render(request, 'approve.html', {'gone': True}, 404)
    c = db.one('SELECT * FROM clients WHERE id=?', d['client_id'])
    agency = db.one('SELECT * FROM agencies WHERE id=?', d['agency_id'])
    body = manifest.pending_body(db, d)
    sha = crypto.hash_policy(body)
    prev = manifest.latest(db, d['client_id'])
    if request.method == 'POST':
        f = await form(request)
        try:
            if f.get('agree') != 'yes':
                raise manifest.ManifestError('Please tick the box to approve')
            v = manifest.approve(db, d['id'], approver=f.get('name', ''), role='client', method='link', expected_sha256=f.get('sha'),
                                 email=f.get('email'), ip=request.client.host if request.client else None)
            return render(request, 'approve.html', {'done': v, 'c': c, 'agency': agency})
        except manifest.ManifestError as e:
            return render(request, 'approve.html', {'c': c, 'agency': agency, 'body': body, 'sha': sha, 'error': str(e),
                                                    'diff': manifest.diff(prev.j('body') if prev else None, body)}, 400)
    return render(request, 'approve.html', {'c': c, 'agency': agency, 'body': body, 'sha': sha,
                                            'diff': manifest.diff(prev.j('body') if prev else None, body), 'prev': prev})


async def manifest_compare(request: Request):
    u = need(request)
    db = get_db()
    c = owned('clients', int(request.path_params['id']), u)
    a, b = int(request.query_params.get('a', 0)), int(request.query_params.get('b', 0))
    va = db.one('SELECT * FROM manifest_versions WHERE client_id=? AND version=?', c['id'], a)
    vb = db.one('SELECT * FROM manifest_versions WHERE client_id=? AND version=?', c['id'], b)
    if not va or not vb:
        raise HTTPException(404)
    return render(request, 'compare.html', {'c': c, 'va': va, 'vb': vb, 'diff': manifest.diff(va.j('body'), vb.j('body'))})


async def integrity(request: Request):
    u = need(request)
    c = owned('clients', int(request.path_params['id']), u)
    return render(request, 'integrity.html', {'c': c, 'r': binding.verify_integrity(get_db(), c['id'])})


# ------------------------------------------------------------------ trials
async def trials_page(request: Request):
    u = need(request)
    db = get_db()
    rows = db.all('SELECT t.*, c.name AS client_name, v.version FROM trials t JOIN clients c ON c.id=t.client_id '
                  'JOIN manifest_versions v ON v.id=t.manifest_version_id WHERE t.agency_id=? ORDER BY t.id DESC', u['agency_id'])
    clients = db.all('SELECT * FROM clients WHERE agency_id=? ORDER BY name', u['agency_id'])
    return render(request, 'trials.html', {'rows': rows, 'clients': clients})


async def trial_create(request: Request):
    u = need(request, 'operator')
    c = owned('clients', int(request.path_params['id']), u)
    f = await form(request)
    db = get_db()
    try:
        tid = trials.create_trial(db, c['id'], runs=int(f.get('runs') or 3), by=u['name'])
        if c['is_sandbox']:
            trials.run_sandbox_trial(db, tid)
            db.run("UPDATE jobs SET done=1 WHERE kind='trial' AND ref_id=?", tid)
        flash(request, 'Trial finished.' if c['is_sandbox'] else 'Trial queued: Vaada is placing the calls.')
        return back(f'/trials/{tid}')
    except ValueError as e:
        flash(request, str(e), 'err')
        return back(f"/clients/{c['id']}/manifest")


def _trial_ctx(db, t):
    rows = db.all('SELECT * FROM trial_results WHERE trial_id=? ORDER BY scenario_id, run_no', t['id'])
    groups: dict = {}
    for r in rows:
        sc = loads(r['scenario'])
        g = groups.setdefault(sc['field'], {})
        g.setdefault(r['scenario_id'], {'sc': sc, 'runs': []})['runs'].append(r)
    s = loads(t['summary'], {})
    v = db.one('SELECT * FROM manifest_versions WHERE id=?', t['manifest_version_id'])
    c = db.one('SELECT * FROM clients WHERE id=?', t['client_id'])
    return {'t': t, 'groups': groups, 's': s, 'v': v, 'c': c}


async def trial_detail(request: Request):
    u = need(request)
    db = get_db()
    t = owned('trials', int(request.path_params['id']), u)
    ctx = _trial_ctx(db, t)
    ctx['share'] = request.session.pop('share_link', None)
    return render(request, 'trial.html', ctx)


async def trial_share(request: Request):
    u = need(request, 'operator')
    t = owned('trials', int(request.path_params['id']), u)
    await form(request)
    tok = trials.share_token(get_db(), t['id'])
    request.session['share_link'] = f'{config.PUBLIC_URL}/t/{tok}'
    return back(f"/trials/{t['id']}")


async def trial_public(request: Request):
    db = get_db()
    t = db.one('SELECT * FROM trials WHERE shared_token=?', crypto.token_hash(request.path_params['token']))
    if not t:
        raise HTTPException(404)
    ctx = _trial_ctx(db, t)
    ctx['public'] = True
    return render(request, 'trial.html', ctx)


# ----------------------------------------------------------------- reports
async def report_page(request: Request):
    u = need(request)
    c = owned('clients', int(request.path_params['id']), u)
    days = int(request.query_params.get('days', 30))
    return render(request, 'report.html', {'r': reports.client_report(get_db(), c['id'], days), 'c': c, 'days': days})


async def report_pdf(request: Request):
    u = need(request)
    c = owned('clients', int(request.path_params['id']), u)
    days = int(request.query_params.get('days', 30))
    pdf = reports.report_pdf(reports.client_report(get_db(), c['id'], days), u['agency_name'])
    name = manifest.re.sub(r'[^a-z0-9]+', '-', c['name'].lower()).strip('-')
    return Response(pdf, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="vaada-{name}-{days}d.pdf"'})


# ---------------------------------------------------------- sandbox CRM UI
async def sandbox_crm(request: Request):
    u = need(request)
    db = get_db()
    c = owned('clients', int(request.path_params['id']), u)
    if not c['is_sandbox']:
        raise HTTPException(404)
    if request.method == 'POST':
        need(request, 'operator')
        f = await form(request)
        if f.get('action') in ('make_task', 'update_contact'):
            from .integrations.highlevel import digits
            cid = f.get('contact_id') or next((r['contact_id'] for r in db.all('SELECT contact_id, phone FROM sandbox_contacts WHERE client_id=?', c['id'])
                                               if digits(r['phone']) and digits(r['phone']) == digits(f.get('phone'))), None)
            if not cid:
                flash(request, 'No sandbox contact matches this caller.', 'err')
            elif f['action'] == 'make_task':
                db.insert('sandbox_tasks', agency_id=c['agency_id'], client_id=c['id'], task_id='task_' + secrets.token_hex(4), contact_id=cid,
                          title='Call back the caller (operator)', created_at=iso(now()), completed=1, completed_at=iso(now()))
                db.insert('sandbox_messages', agency_id=c['agency_id'], client_id=c['id'], message_id='call_' + secrets.token_hex(4),
                          contact_id=cid, direction='outbound', channel='call', body='Outbound call by operator, customer reached', sent_at=iso(now()))
                flash(request, 'Callback task created, the call logged and the task closed in the sandbox CRM. Now press Re-check.')
            else:
                db.update('sandbox_contacts', {'contact_id': cid}, **{f.get('field') or 'email': f.get('value')})
                flash(request, 'Contact updated in the sandbox CRM. Now press Re-check.')
            return back(f.get('return') or f"/sandbox/{c['id']}")
        if f.get('action') == 'send_text':
            from .integrations.highlevel import digits
            cid = f.get('contact_id') or next((r['contact_id'] for r in db.all('SELECT contact_id, phone FROM sandbox_contacts WHERE client_id=?', c['id'])
                                               if digits(r['phone']) and digits(r['phone']) == digits(f.get('phone'))), None)
            if cid:
                db.insert('sandbox_messages', agency_id=c['agency_id'], client_id=c['id'], message_id='msg_' + secrets.token_hex(4),
                          contact_id=cid, direction='outbound', channel='sms', body=_confirmation_body(db, c, f.get('finding_id')), sent_at=iso(now()))
                flash(request, 'Text sent in the sandbox conversation. Now press Re-check.')
            else:
                flash(request, 'No sandbox contact matches this caller.', 'err')
            return back(f.get('return') or f"/sandbox/{c['id']}")
        appt = db.one('SELECT * FROM sandbox_appointments WHERE appointment_id=? AND client_id=?', f.get('appointment_id'), c['id'])
        if appt:
            try:
                from .timeparse import unique_local_wall
                local = datetime.fromisoformat(f.get('start_local'))
                start = unique_local_wall(local, c['timezone'])
                db.update('sandbox_appointments', {'id': appt['id']}, start_at=iso(start), status=f.get('status') or appt['status'],
                          updated_at=iso(now()), written_at=None)
                flash(request, 'Sandbox calendar updated (this stands in for fixing it in HighLevel). Now press Re-check on the case.')
            except Exception as e:
                flash(request, f'Could not update: {e}', 'err')
        return back(f.get('return') or f"/sandbox/{c['id']}")
    rows = db.all('SELECT a.*, s.name AS contact_name FROM sandbox_appointments a LEFT JOIN sandbox_contacts s ON s.contact_id=a.contact_id '
                  'WHERE a.client_id=? ORDER BY a.updated_at DESC LIMIT 60', c['id'])
    return render(request, 'sandbox.html', {'c': c, 'rows': rows, 'focus': request.query_params.get('appt')})


# ---------------------------------------------------------------- settings
async def settings_page(request: Request):
    u = need(request)
    db = get_db()
    agency = db.one('SELECT * FROM agencies WHERE id=?', u['agency_id'])
    new_key = None
    if request.method == 'POST':
        need(request, 'owner')
        f = await form(request)
        act = f.get('action')
        try:
            if act == 'vapi':
                db.update('agencies', {'id': agency['id']}, vapi_secret_enc=crypto.encrypt(f.get('vapi_secret', '').strip()) if f.get('vapi_secret', '').strip() else agency['vapi_secret_enc'],
                          vapi_api_key_enc=crypto.encrypt(f.get('vapi_key', '').strip()) if f.get('vapi_key', '').strip() else agency['vapi_api_key_enc'])
                flash(request, 'Vapi credentials saved (encrypted).')
            if act == 'retell':
                db.update('agencies', {'id': agency['id']}, retell_api_key_enc=crypto.encrypt(f.get('retell_key', '').strip()))
                flash(request, 'Retell API key saved (encrypted). It verifies webhooks and reads agent versions.')
            elif act == 'highlevel':
                db.update('agencies', {'id': agency['id']}, highlevel_token_enc=crypto.encrypt(f.get('hl_token', '').strip()))
                flash(request, 'HighLevel token saved (encrypted). Use read-only scopes only.')
            elif act == 'apikey':
                new_key = accounts.set_api_key(db, agency['id'])
            elif act == 'user':
                accounts.add_user(db, agency['id'], f.get('email', ''), f.get('name', ''), f.get('role', 'operator'), f.get('password', ''))
                flash(request, 'Teammate added.')
        except accounts.AccountError as e:
            flash(request, str(e), 'err')
        if not new_key:
            return back('/settings')
    users = db.all('SELECT * FROM users WHERE agency_id=?', agency['id'])
    keys = db.all('SELECT key_id, public_key, created_at FROM signing_keys WHERE agency_id=?', agency['id'])
    hb = db.one("SELECT * FROM heartbeats WHERE name='worker'")
    return render(request, 'settings.html', {'agency': agency, 'users': users, 'keys': keys, 'new_key': new_key,
                                             'webhook': f"{config.PUBLIC_URL}/webhooks/retell/{agency['webhook_key']}",
                                             'hb': hb, 'has_retell': bool(agency['retell_api_key_enc']), 'has_vapi': bool(agency['vapi_secret_enc']), 'vapi_webhook': f"{config.PUBLIC_URL}/webhooks/vapi/{agency['webhook_key']}",
                                             'has_hl': bool(agency['highlevel_token_enc'])})


# ----------------------------------------------------------------- webhook
async def _handle_webhook(agency, body: bytes, sig: str | None) -> dict:
    db = get_db()
    key = crypto.decrypt(agency['retell_api_key_enc'])
    if not retell.verify_signature(body, key or '', sig):
        db.insert('webhook_events', agency_id=agency['id'], event=None, call_id=None, signature_ok=0, outcome='bad_signature', received_at=iso(now()))
        return {'status': 'rejected', 'reason': 'invalid signature', '_code': 401}
    try:
        payload = json.loads(body)
    except ValueError:
        return {'status': 'rejected', 'reason': 'invalid json', '_code': 400}
    return pipeline.ingest(db, agency['id'], payload)


async def webhook_vapi(request: Request):
    """Vapi server URL. Auth: Bearer / X-Vapi-Secret / HMAC x-signature, all against the agency's Vapi secret."""
    body = await request.body()
    if len(body) > 5_000_000:
        return JSONResponse({'error': 'too large'}, 413)
    db = get_db()
    agency = db.one('SELECT * FROM agencies WHERE webhook_key=?', request.path_params['key'])
    if not agency:
        return JSONResponse({'error': 'unknown endpoint'}, 404)
    if not vapi.verify(dict(request.headers), body, crypto.decrypt(agency['vapi_secret_enc']) if agency['vapi_secret_enc'] else None):
        db.insert('webhook_events', agency_id=agency['id'], event=None, call_id=None, signature_ok=0, outcome='bad_signature', received_at=iso(now()))
        return JSONResponse({'status': 'rejected', 'reason': 'invalid credentials'}, 401)
    try:
        payload = vapi.to_ingest_payload(json.loads(body))
    except (ValueError, AttributeError):
        return JSONResponse({'status': 'rejected', 'reason': 'invalid json'}, 400)
    res = pipeline.ingest(db, agency['id'], payload, source='vapi')
    return JSONResponse(res, 200 if res.get('status') != 'rejected' else 400)


async def webhook(request: Request):
    body = await request.body()
    if len(body) > 5_000_000:
        return JSONResponse({'error': 'too large'}, 413)
    agency = get_db().one('SELECT * FROM agencies WHERE webhook_key=?', request.path_params['key'])
    if not agency:
        return JSONResponse({'error': 'unknown endpoint'}, 404)
    res = await _handle_webhook(agency, body, request.headers.get('x-retell-signature'))
    code = res.pop('_code', 200)
    return JSONResponse(res, code)


# --------------------------------------------------------------------- API
def api_agency(request: Request):
    auth = request.headers.get('authorization', '')
    a = accounts.agency_by_api_key(get_db(), auth[7:] if auth.lower().startswith('bearer ') else '')
    if not a:
        raise HTTPException(401, 'Bad API key')
    return a


async def api_findings(request: Request):
    a = api_agency(request)
    rows = _finding_rows(get_db(), a['id'], limit=500)
    return JSONResponse([{'id': r['id'], 'client': r['client_name'], 'type': r['type'], 'state': r['state'], 'severity': r['severity'],
                          'reason': r['reason'], 'manifest_version': r['manifest_version'], 'manifest_sha256': r['manifest_sha256'],
                          'agent_version': r['agent_version'], 'agent_changed': bool(r['agent_changed']),
                          'call_id': r['retell_call_id'], 'case_status': r['case_status']} for r in rows])


async def api_recheck(request: Request):
    a = api_agency(request)
    db = get_db()
    f = db.one("SELECT * FROM findings WHERE id=? AND agency_id=? AND type='booking'", int(request.path_params['id']), a['id'])
    if not f:
        raise HTTPException(404)
    with db.tx():
        out = verify.check(db, f['id'], 'manual', actor='api')
    return JSONResponse({'id': out['id'], 'state': out['state'], 'reason': out['reason']})


async def health(request: Request):
    db = get_db()
    hb = db.one("SELECT * FROM heartbeats WHERE name='worker'")
    age = (now() - parse_iso(hb['at'])).total_seconds() if hb else None
    due = db.val('SELECT COUNT(*) FROM jobs WHERE done=0 AND run_at<=?', iso(now()))
    return JSONResponse({'web': 'ok', 'worker': 'ok' if age is not None and age < 30 else 'stale', 'worker_age_s': age, 'due_jobs': due})


async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers['Location'], 303)
    if request.url.path.startswith(('/api/', '/webhooks/')):
        return JSONResponse({'error': exc.detail}, exc.status_code)
    return render(request, 'error.html', {'code': exc.status_code, 'detail': exc.detail}, exc.status_code)


async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    if not request.url.path.startswith('/static'):
        resp.headers.setdefault('Cache-Control', 'no-store')
    return resp


def create_app() -> Starlette:
    from starlette.middleware.base import BaseHTTPMiddleware
    routes = [
        Route('/', inbox), Route('/login', login, methods=['GET', 'POST']), Route('/signup', signup, methods=['GET', 'POST']),
        Route('/logout', logout, methods=['POST']), Route('/demo', demo_login, methods=['POST']),
        Route('/findings', findings_queue), Route('/findings/{id:int}', finding_detail),
        Route('/findings/{id:int}/review', finding_review, methods=['POST']), Route('/cases/{id:int}/act', case_act, methods=['POST']),
        Route('/calls/{id:int}', call_detail),
        Route('/clients', clients_page, methods=['GET', 'POST']), Route('/clients/{id:int}', client_detail),
        Route('/clients/{id:int}/simulate', client_simulate, methods=['POST']), Route('/clients/{id:int}/sync', client_sync, methods=['POST']),
        Route('/clients/{id:int}/manifest', manifest_page), Route('/clients/{id:int}/drafts', draft_create, methods=['POST']),
        Route('/drafts/{id:int}/{action:str}', draft_action, methods=['POST']),
        Route('/clients/{id:int}/compare', manifest_compare), Route('/clients/{id:int}/integrity', integrity),
        Route('/clients/{id:int}/trials', trial_create, methods=['POST']), Route('/trials', trials_page),
        Route('/trials/{id:int}', trial_detail), Route('/trials/{id:int}/share', trial_share, methods=['POST']),
        Route('/t/{token:str}', trial_public), Route('/approve/{token:str}', approve_public, methods=['GET', 'POST']),
        Route('/reports/{id:int}', report_page), Route('/reports/{id:int}/pdf', report_pdf),
        Route('/sandbox/{id:int}', sandbox_crm, methods=['GET', 'POST']), Route('/settings', settings_page, methods=['GET', 'POST']),
        Route('/webhooks/retell/{key:str}', webhook, methods=['POST']),
        Route('/webhooks/vapi/{key:str}', webhook_vapi, methods=['POST']),
        Route('/api/v1/findings', api_findings), Route('/api/v1/findings/{id:int}/recheck', api_recheck, methods=['POST']),
        Route('/health', health), Mount('/static', StaticFiles(directory=str(HERE / 'static')), name='static'),
    ]

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        get_db()
        if config.DEMO_MODE and config.flag('VAADA_SEED_ON_START', True):
            from . import seed
            seed.seed(get_db(), quiet=True)
        stop = worker.start_thread() if config.INLINE_WORKER else None
        yield
        if stop:
            stop.set()

    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: http_error},
                    middleware=[Middleware(SessionMiddleware, secret_key=config.secret_key(), session_cookie='vaada_session',
                                           max_age=60 * 60 * 12, same_site='lax', https_only=config.SESSION_HTTPS_ONLY),
                                Middleware(BaseHTTPMiddleware, dispatch=security_headers)])
    return app


app = create_app()
