"""HighLevel record access. Read-only by construction: this module has no
POST/PUT/PATCH/DELETE. The sandbox CRM is a local stand-in for demos and trials."""
from __future__ import annotations

from .. import http as httpx

from .. import config, crypto
from ..db import Database, iso, now, parse_iso
from ..timeparse import to_utc


class CRMError(Exception):
    def __init__(self, msg, kind='error'):
        super().__init__(msg)
        self.kind = kind  # auth | scope | network | not_found | error


def norm_appt(a: dict, tz: str, contact_hint: str | None = None) -> dict:
    st = a.get('startTime') or a.get('start_time') or a.get('start_at')
    try:
        start = iso(to_utc(st, tz)) if st else None
    except ValueError:
        start = None
    return {'id': a.get('id') or a.get('appointment_id'), 'contact_id': a.get('contactId') or a.get('contact_id') or contact_hint,
            'calendar_id': a.get('calendarId') or a.get('calendar_id'),
            'start_at': start, 'status': str(a.get('appointmentStatus') or a.get('status') or '').lower() or None,
            'title': a.get('title'), 'created_at': _iso_or_none(a.get('dateAdded') or a.get('created_at'), tz)}


def _iso_or_none(v, tz):
    try:
        return iso(to_utc(v, tz)) if v else None
    except ValueError:
        return None


def digits(phone: str | None) -> str:
    d = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    return d[-10:]


class HighLevelCRM:
    VERSIONS = {'calendars': config.HIGHLEVEL_VERSION, 'contacts': '2021-07-28', 'locations': '2021-07-28',
                'conversations': '2021-04-15'}

    def __init__(self, token: str, location_id: str, tz: str):
        if not token:
            raise CRMError('HighLevel token not configured for this client', 'auth')
        self.token, self.location_id, self.tz = token, location_id, tz

    def _get(self, path: str, family: str, params=None):
        h = {'Authorization': f'Bearer {self.token}', 'Version': self.VERSIONS[family], 'Accept': 'application/json'}
        try:
            r = httpx.get(config.HIGHLEVEL_BASE + path, headers=h, params=params, timeout=12)
        except httpx.HTTPError as e:
            raise CRMError(f'network: {type(e).__name__}', 'network')
        if r.status_code == 404:
            return None
        if r.status_code in (401, 403):
            raise CRMError(f'HighLevel HTTP {r.status_code}: token revoked or missing read scope', 'auth')
        if r.status_code == 429:
            raise CRMError('HighLevel rate limit', 'network')
        if r.status_code != 200:
            raise CRMError(f'HighLevel HTTP {r.status_code}', 'error')
        try:
            return r.json()
        except ValueError:
            raise CRMError('invalid JSON from HighLevel')

    def appointment(self, appt_id: str) -> dict | None:
        j = self._get(f'/calendars/events/appointments/{appt_id}', 'calendars')
        a = (j or {}).get('appointment') or (j or {}).get('event')
        return norm_appt(a, self.tz) if a else None

    def contact_appointments(self, contact_id: str) -> list[dict]:
        j = self._get(f'/contacts/{contact_id}/appointments', 'contacts') or {}
        return [norm_appt(e, self.tz, contact_id) for e in j.get('events', []) if isinstance(e, dict)]

    def contact_in_location(self, contact_id: str) -> bool:
        j = self._get(f'/contacts/{contact_id}', 'contacts')
        return bool(j and (j.get('contact') or {}).get('locationId') == self.location_id)

    def contact_phone(self, contact_id: str) -> str | None:
        j = self._get(f'/contacts/{contact_id}', 'contacts') or {}
        return (j.get('contact') or {}).get('phone')

    def contacts_by_phone(self, phone: str) -> list[str]:
        """Identity linking: other contact records in this sub-account with the same phone."""
        if not digits(phone):
            return []
        j = self._get('/contacts/', 'contacts', {'locationId': self.location_id, 'query': digits(phone), 'limit': 20}) or {}
        return [c['id'] for c in j.get('contacts', []) if digits(c.get('phone')) == digits(phone)]

    def contact(self, contact_id: str) -> dict | None:
        c = (self._get(f'/contacts/{contact_id}', 'contacts') or {}).get('contact')
        return {'id': c.get('id'), 'email': c.get('email'), 'phone': c.get('phone')} if c else None

    def tasks(self, contact_id: str) -> list[dict]:
        j = self._get(f'/contacts/{contact_id}/tasks', 'contacts') or {}
        return [{'id': t.get('id'), 'title': t.get('title'), 'created_at': _iso_or_none(t.get('dateAdded'), self.tz),
                 'due_at': _iso_or_none(t.get('dueDate'), self.tz), 'completed': bool(t.get('completed'))} for t in j.get('tasks', [])]

    def messages(self, contact_id: str) -> list[dict]:
        """Outbound/inbound messages for a contact (read-only conversations API)."""
        out = []
        convs = (self._get('/conversations/search', 'conversations', {'locationId': self.location_id, 'contactId': contact_id}) or {}).get('conversations') or []
        for c in convs[:5]:
            j = self._get(f"/conversations/{c['id']}/messages", 'conversations') or {}
            msgs = j.get('messages', {})
            msgs = msgs.get('messages', []) if isinstance(msgs, dict) else msgs
            for m in msgs:
                typ = str(m.get('messageType') or m.get('type') or '').upper()
                out.append({'id': m.get('id'), 'contact_id': contact_id, 'direction': (m.get('direction') or '').lower(),
                            'channel': 'sms' if 'SMS' in typ else ('email' if 'EMAIL' in typ else typ.lower()),
                            'body': (m.get('body') or '')[:300], 'status': (m.get('status') or '').lower() or None,
                            'sent_at': _iso_or_none(m.get('dateAdded'), self.tz)})
        return out

    def location_settings(self) -> dict:
        loc = (self._get(f'/locations/{self.location_id}', 'locations') or {}).get('location') or {}
        cals = (self._get('/calendars/', 'calendars', {'locationId': self.location_id}) or {}).get('calendars') or []
        return {'name': loc.get('name'), 'timezone': loc.get('timezone'),
                'calendars': [{'id': c.get('id'), 'name': c.get('name'), 'slot_minutes': c.get('slotDuration')} for c in cals]}

    def ping(self) -> str:
        self._get(f'/locations/{self.location_id}', 'locations')
        return 'connected'


class SandboxCRM:
    """Local stand-in for a HighLevel sub-account. Writes only come from the
    simulated agent or the operator's 'fix in sandbox' screen, never from Vaada's checker."""

    def __init__(self, db: Database, client: dict):
        self.db, self.client = db, client

    def _visible(self, r):
        return not r['written_at'] or parse_iso(r['written_at']) <= now()

    def appointment(self, appt_id: str):
        r = self.db.one('SELECT * FROM sandbox_appointments WHERE appointment_id=? AND client_id=?', appt_id, self.client['id'])
        if not r or not self._visible(r):
            return None
        return {'id': r['appointment_id'], 'contact_id': r['contact_id'], 'calendar_id': r['calendar_id'],
                'start_at': r['start_at'], 'status': r['status'], 'title': r['title'], 'created_at': r.get('created_at')}

    def contact_appointments(self, contact_id: str):
        rows = self.db.all('SELECT * FROM sandbox_appointments WHERE contact_id=? AND client_id=?', contact_id, self.client['id'])
        return [{'id': r['appointment_id'], 'contact_id': r['contact_id'], 'calendar_id': r['calendar_id'],
                 'start_at': r['start_at'], 'status': r['status'], 'title': r['title'], 'created_at': r.get('created_at')}
                for r in rows if self._visible(r)]

    def contact_phone(self, contact_id: str):
        return self.db.val('SELECT phone FROM sandbox_contacts WHERE contact_id=? AND client_id=?', contact_id, self.client['id'])

    def contacts_by_phone(self, phone: str) -> list[str]:
        if not digits(phone):
            return []
        return [r['contact_id'] for r in self.db.all('SELECT contact_id, phone FROM sandbox_contacts WHERE client_id=?', self.client['id'])
                if digits(r['phone']) == digits(phone)]

    def contact(self, contact_id: str):
        r = self.db.one('SELECT contact_id, email, phone FROM sandbox_contacts WHERE contact_id=? AND client_id=?', contact_id, self.client['id'])
        return {'id': r['contact_id'], 'email': r['email'], 'phone': r['phone']} if r else None

    def tasks(self, contact_id: str):
        return [{'id': r['task_id'], 'title': r['title'], 'created_at': r['created_at'], 'due_at': r['due_at'], 'completed': bool(r['completed'])}
                for r in self.db.all('SELECT * FROM sandbox_tasks WHERE contact_id=? AND client_id=?', contact_id, self.client['id'])]

    def messages(self, contact_id: str):
        return [dict(r) for r in self.db.all('SELECT message_id AS id, contact_id, direction, channel, body, sent_at FROM sandbox_messages '
                                             'WHERE contact_id=? AND client_id=?', contact_id, self.client['id'])]

    def contact_in_location(self, contact_id: str) -> bool:
        return bool(self.db.one('SELECT 1 FROM sandbox_contacts WHERE contact_id=? AND client_id=?', contact_id, self.client['id']))

    def ping(self):
        return 'connected'


def crm_for(db: Database, client: dict):
    if client.get('is_sandbox'):
        return SandboxCRM(db, client)
    token = crypto.decrypt(client.get('hl_token_enc'))
    if not token:
        token = crypto.decrypt(db.val('SELECT highlevel_token_enc FROM agencies WHERE id=?', client['agency_id']))
    return HighLevelCRM(token or '', client.get('hl_location_id') or '', client['timezone'])
