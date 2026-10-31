"""Agencies, users and clients."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from . import crypto
from .db import Database, dumps, iso, now, slugify
from .manifest import get_signing_key


class AccountError(ValueError):
    pass


def create_agency(db: Database, name: str, owner_email: str, owner_name: str, password: str, *, sandbox=False) -> int:
    if len(password) < 10:
        raise AccountError('Use a password of at least 10 characters')
    email = owner_email.strip().lower()
    if db.one('SELECT 1 FROM users WHERE email=?', email):
        raise AccountError('That email already has an account')
    base = slugify(name)
    slug, n = base, 1
    while db.one('SELECT 1 FROM agencies WHERE slug=?', slug):
        n += 1
        slug = f'{base}-{n}'
    with db.tx():
        aid = db.insert('agencies', name=name.strip()[:120], slug=slug, webhook_key=crypto.token()[:32],
                        is_sandbox=1 if sandbox else 0, created_at=iso(now()))
        db.insert('users', agency_id=aid, email=email, name=owner_name.strip()[:80] or email, role='owner',
                  pw_hash=crypto.hash_password(password), created_at=iso(now()))
        get_signing_key(db, aid)
    return aid


def add_user(db: Database, agency_id: int, email: str, name: str, role: str, password: str):
    if role not in ('owner', 'operator', 'viewer'):
        raise AccountError('Unknown role')
    if db.one('SELECT 1 FROM users WHERE email=?', email.strip().lower()):
        raise AccountError('That email already has an account')
    db.insert('users', agency_id=agency_id, email=email.strip().lower(), name=name.strip()[:80], role=role,
              pw_hash=crypto.hash_password(password), created_at=iso(now()))


def authenticate(db: Database, email: str, password: str):
    u = db.one('SELECT * FROM users WHERE email=?', (email or '').strip().lower())
    if u and crypto.check_password(password or '', u['pw_hash']):
        return u
    return None


def create_client(db: Database, agency_id: int, *, name: str, retell_agent_id: str, timezone: str, location_label='',
                  hl_location_id='', calendars=None, hl_token: str | None = None, sandbox=False, snapshot=None) -> int:
    if not name.strip() or not retell_agent_id.strip():
        raise AccountError('Client name and Retell agent ID are required')
    try:
        ZoneInfo(timezone)
    except Exception:
        raise AccountError('Unknown IANA timezone (e.g. America/Chicago)')
    if db.one('SELECT 1 FROM clients WHERE agency_id=? AND retell_agent_id=?', agency_id, retell_agent_id.strip()):
        raise AccountError('That Retell agent is already registered to a client')
    cals = calendars or []
    return db.insert('clients', agency_id=agency_id, name=name.strip()[:120], location_label=location_label[:120],
                     hl_location_id=hl_location_id.strip() or None, retell_agent_id=retell_agent_id.strip(), timezone=timezone,
                     calendars=dumps(cals), hl_token_enc=crypto.encrypt(hl_token) if hl_token else None,
                     connection_status='sandbox' if sandbox else 'not_tested', agent_snapshot=dumps(snapshot) if snapshot else None,
                     is_sandbox=1 if sandbox else 0, created_at=iso(now()))


def set_api_key(db: Database, agency_id: int) -> str:
    k = 'vaada_' + crypto.token()
    db.update('agencies', {'id': agency_id}, api_key_hash=crypto.token_hash(k))
    return k


def agency_by_api_key(db: Database, key: str):
    return db.one('SELECT * FROM agencies WHERE api_key_hash=?', crypto.token_hash(key)) if key else None
