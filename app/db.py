"""Storage layer: SQLite for dev/demo, Postgres for production (psycopg 3).

Rules the schema enforces (spec, "Data model"):
  * every row carries agency_id; every query in services filters on it (tenant isolation)
  * evidence is never edited in place: approved manifest versions, signature envelopes,
    approvals, calls, observations, binding receipts and case events are append-only and
    protected by database triggers
  * the request (finding/commitment) is kept apart from the attempts to fulfil it (observations)
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

IMMUTABLE = ['ledger_entries', 'manifest_versions', 'signature_envelopes', 'approvals', 'calls', 'observations',
             'binding_receipts', 'case_events', 'trial_results', 'webhook_events', 'audit_log']

SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (
  id {pk}, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, webhook_key TEXT UNIQUE NOT NULL,
  retell_api_key_enc TEXT, highlevel_token_enc TEXT, api_key_hash TEXT,
  plan TEXT DEFAULT 'trial', is_sandbox INTEGER DEFAULT 0, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
  id {pk}, agency_id INTEGER NOT NULL REFERENCES agencies(id), email TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'operator', pw_hash TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS signing_keys (
  id {pk}, agency_id INTEGER NOT NULL REFERENCES agencies(id), key_id TEXT UNIQUE NOT NULL,
  public_key TEXT NOT NULL, private_key_enc TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);

CREATE TABLE IF NOT EXISTS clients (
  id {pk}, agency_id INTEGER NOT NULL REFERENCES agencies(id), name TEXT NOT NULL, location_label TEXT,
  hl_location_id TEXT, retell_agent_id TEXT NOT NULL, timezone TEXT NOT NULL,
  calendars TEXT NOT NULL DEFAULT '[]', hl_token_enc TEXT,
  connection_status TEXT NOT NULL DEFAULT 'not_tested', connection_checked_at TEXT, connection_error TEXT,
  agent_snapshot TEXT, is_sandbox INTEGER DEFAULT 0, created_at TEXT NOT NULL,
  UNIQUE(agency_id, retell_agent_id));

CREATE TABLE IF NOT EXISTS manifest_drafts (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL REFERENCES clients(id),
  base_version INTEGER, base_sha256 TEXT, policy TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
  state TEXT NOT NULL DEFAULT 'draft', approval_token_hash TEXT, approval_expires_at TEXT,
  sent_to TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS manifest_versions (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL REFERENCES clients(id),
  version INTEGER NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL, prev_hash TEXT,
  approved_agent_version TEXT NOT NULL, agent_fingerprint TEXT, effective_from TEXT NOT NULL,
  draft_id INTEGER, created_at TEXT NOT NULL, UNIQUE(client_id, version), UNIQUE(client_id, sha256));

CREATE TABLE IF NOT EXISTS signature_envelopes (
  id {pk}, agency_id INTEGER NOT NULL, manifest_version_id INTEGER UNIQUE NOT NULL REFERENCES manifest_versions(id),
  manifest_sha256 TEXT NOT NULL, alg TEXT NOT NULL, signature TEXT NOT NULL, key_id TEXT NOT NULL, signed_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS approvals (
  id {pk}, agency_id INTEGER NOT NULL, manifest_version_id INTEGER UNIQUE NOT NULL REFERENCES manifest_versions(id),
  approved_sha256 TEXT NOT NULL, approver_name TEXT NOT NULL, approver_email TEXT, role TEXT NOT NULL,
  method TEXT NOT NULL, approved_at TEXT NOT NULL, ip TEXT);

CREATE TABLE IF NOT EXISTS calls (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL REFERENCES clients(id),
  retell_call_id TEXT UNIQUE NOT NULL, agent_id TEXT, agent_version TEXT NOT NULL, agent_fingerprint TEXT,
  started_at TEXT, ended_at TEXT, transcript TEXT NOT NULL, turns TEXT NOT NULL, tool_calls TEXT NOT NULL,
  meta TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'retell', trial_result_ref TEXT, received_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS call_state (
  call_id INTEGER PRIMARY KEY REFERENCES calls(id), agency_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'ingested', coverage TEXT, binding_receipt_id INTEGER,
  manifest_version_id INTEGER, manifest_sha256 TEXT, binding_status TEXT, drift_class TEXT,
  error TEXT, processed_at TEXT);

CREATE TABLE IF NOT EXISTS binding_receipts (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL, call_id INTEGER UNIQUE NOT NULL,
  seq INTEGER NOT NULL, prev_receipt_hash TEXT, receipt_hash TEXT NOT NULL, payload TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(client_id, seq));

CREATE TABLE IF NOT EXISTS findings (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL, call_id INTEGER REFERENCES calls(id),
  type TEXT NOT NULL, severity TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
  rule TEXT NOT NULL DEFAULT '{{}}', span TEXT NOT NULL DEFAULT '{{}}', promised TEXT NOT NULL DEFAULT '{{}}',
  manifest_version INTEGER, manifest_sha256 TEXT, signature_key_id TEXT, agent_version TEXT,
  agent_changed INTEGER DEFAULT 0, boundary_note TEXT, dedupe_key TEXT UNIQUE,
  superseded_by INTEGER, dismissed_reason TEXT, reviewed_by TEXT, reviewed_at TEXT,
  appointment_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS observations (
  id {pk}, agency_id INTEGER NOT NULL, finding_id INTEGER NOT NULL REFERENCES findings(id),
  appointment_id TEXT, fields TEXT NOT NULL, verdict TEXT NOT NULL, reason TEXT NOT NULL,
  stage TEXT NOT NULL, source TEXT NOT NULL, read_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS cases (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL, finding_id INTEGER UNIQUE NOT NULL REFERENCES findings(id),
  status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'P3', owner TEXT,
  due_at TEXT, customer_contact TEXT NOT NULL DEFAULT 'not_needed', fixed_at TEXT, closed_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS case_events (
  id {pk}, agency_id INTEGER NOT NULL, case_id INTEGER NOT NULL REFERENCES cases(id),
  actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS trials (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL, manifest_version_id INTEGER NOT NULL,
  manifest_sha256 TEXT NOT NULL, agent_version TEXT NOT NULL, mode TEXT NOT NULL, runs_per_scenario INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', summary TEXT NOT NULL DEFAULT '{{}}', shared_token TEXT,
  created_by TEXT, created_at TEXT NOT NULL, finished_at TEXT);

CREATE TABLE IF NOT EXISTS trial_results (
  id {pk}, agency_id INTEGER NOT NULL, trial_id INTEGER NOT NULL REFERENCES trials(id), scenario_id TEXT NOT NULL,
  scenario TEXT NOT NULL, run_no INTEGER NOT NULL, call_id INTEGER, verdict TEXT NOT NULL, reason TEXT NOT NULL,
  expected TEXT NOT NULL, actual TEXT NOT NULL, clip TEXT, at TEXT NOT NULL, UNIQUE(trial_id, scenario_id, run_no));

CREATE TABLE IF NOT EXISTS jobs (
  id {pk}, agency_id INTEGER NOT NULL, kind TEXT NOT NULL, ref_id INTEGER NOT NULL, stage TEXT NOT NULL,
  run_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, locked_until TEXT, UNIQUE(kind, ref_id, stage));

CREATE TABLE IF NOT EXISTS webhook_events (
  id {pk}, agency_id INTEGER NOT NULL, event TEXT, call_id TEXT, signature_ok INTEGER NOT NULL,
  outcome TEXT NOT NULL, received_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sandbox_appointments (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL, appointment_id TEXT UNIQUE NOT NULL,
  contact_id TEXT NOT NULL, calendar_id TEXT NOT NULL, start_at TEXT NOT NULL, status TEXT NOT NULL,
  title TEXT, updated_at TEXT NOT NULL, written_at TEXT);

CREATE TABLE IF NOT EXISTS sandbox_contacts (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL, contact_id TEXT UNIQUE NOT NULL,
  name TEXT, phone TEXT, location_id TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sandbox_messages (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL, message_id TEXT UNIQUE NOT NULL,
  contact_id TEXT NOT NULL, direction TEXT NOT NULL, channel TEXT NOT NULL, body TEXT, sent_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sandbox_tasks (
  id {pk}, agency_id INTEGER NOT NULL, client_id INTEGER NOT NULL, task_id TEXT UNIQUE NOT NULL, contact_id TEXT NOT NULL,
  title TEXT, created_at TEXT NOT NULL, due_at TEXT, completed INTEGER NOT NULL DEFAULT 0, completed_at TEXT);

CREATE TABLE IF NOT EXISTS ledger_entries (
  id {pk}, agency_id INTEGER NOT NULL, finding_id INTEGER NOT NULL REFERENCES findings(id), intent_key TEXT NOT NULL,
  kind TEXT NOT NULL, expected TEXT NOT NULL, effects TEXT NOT NULL, attempts INTEGER NOT NULL, tool_reported TEXT,
  verdict TEXT NOT NULL, note TEXT, stage TEXT NOT NULL, at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit_log (
  id {pk}, agency_id INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
  entity_id TEXT, detail TEXT NOT NULL, at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS heartbeats (name TEXT PRIMARY KEY, at TEXT NOT NULL, detail TEXT);

CREATE INDEX IF NOT EXISTS ix_findings_agency_state ON findings(agency_id, state);
CREATE INDEX IF NOT EXISTS ix_calls_client ON calls(client_id, started_at);
CREATE INDEX IF NOT EXISTS ix_jobs_due ON jobs(done, run_at);
CREATE INDEX IF NOT EXISTS ix_mv_client ON manifest_versions(client_id, effective_from);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def loads(s, default=None):
    if s is None or s == '':
        return default
    return json.loads(s)


class Row(dict):
    """dict with attribute access; JSON columns decoded lazily with .j()."""
    __getattr__ = dict.get

    def j(self, key, default=None):
        v = self.get(key)
        return loads(v, default) if isinstance(v, str) else (v if v is not None else default)


class Database:
    def __init__(self, url: str):
        self.url = url
        self.kind = 'postgres' if url.startswith(('postgres://', 'postgresql://')) else 'sqlite'
        self._local = threading.local()
        self.write_lock = threading.RLock()

    # -- connections -----------------------------------------------------
    def _connect(self):
        if self.kind == 'sqlite':
            path = self.url.replace('sqlite:///', '', 1)
            conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=30000')
            return conn
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
        return psycopg.connect(self.url, autocommit=True, row_factory=dict_row)

    @property
    def conn(self):
        c = getattr(self._local, 'conn', None)
        if c is None:
            c = self._local.conn = self._connect()
            self._local.depth = 0
        return c

    def _sql(self, sql: str) -> str:
        if self.kind == 'postgres':
            sql = sql.replace('?', '%s')
        return sql

    @contextmanager
    def tx(self):
        """Nestable transaction. SQLite uses BEGIN IMMEDIATE so writers serialize."""
        depth = getattr(self._local, 'depth', 0)
        c = self.conn
        if depth == 0:
            self.write_lock.acquire()
            c.execute('BEGIN IMMEDIATE' if self.kind == 'sqlite' else 'BEGIN')
        self._local.depth = depth + 1
        try:
            yield self
            self._local.depth -= 1
            if self._local.depth == 0:
                c.execute('COMMIT')
                self.write_lock.release()
        except BaseException:
            self._local.depth -= 1
            if self._local.depth == 0:
                try:
                    c.execute('ROLLBACK')
                finally:
                    self.write_lock.release()
            raise

    # -- queries ---------------------------------------------------------
    def all(self, sql: str, *args) -> list[Row]:
        cur = self.conn.execute(self._sql(sql), args)
        return [Row(dict(r)) for r in cur.fetchall()]

    def one(self, sql: str, *args) -> Row | None:
        cur = self.conn.execute(self._sql(sql), args)
        r = cur.fetchone()
        return Row(dict(r)) if r else None

    def val(self, sql: str, *args):
        r = self.one(sql, *args)
        return next(iter(r.values())) if r else None

    def run(self, sql: str, *args):
        return self.conn.execute(self._sql(sql), args)

    def insert(self, table: str, **cols) -> int:
        keys = list(cols)
        vals = [dumps(v) if isinstance(v, (dict, list)) else v for v in cols.values()]
        sql = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})"
        if self.kind == 'postgres':
            if table in ('call_state', 'heartbeats'):  # natural primary keys, no id column
                self.conn.execute(self._sql(sql), vals)
                return None
            r = self.conn.execute(self._sql(sql + ' RETURNING id'), vals).fetchone()
            return r['id']
        cur = self.conn.execute(sql, vals)
        return cur.lastrowid

    def update(self, table: str, where: dict, **cols):
        sets = ','.join(f'{k}=?' for k in cols)
        conds = ' AND '.join(f'{k}=?' for k in where)
        vals = [dumps(v) if isinstance(v, (dict, list)) else v for v in cols.values()] + list(where.values())
        return self.run(f'UPDATE {table} SET {sets} WHERE {conds}', *vals)

    # -- schema ----------------------------------------------------------
    def init(self):
        pk = 'INTEGER PRIMARY KEY AUTOINCREMENT' if self.kind == 'sqlite' else 'BIGSERIAL PRIMARY KEY'
        ddl = SCHEMA.format(pk=pk)
        if self.kind == 'postgres':
            ddl = ddl.replace('INTEGER PRIMARY KEY REFERENCES', 'BIGINT PRIMARY KEY REFERENCES')
        for stmt in [s.strip() for s in ddl.split(';') if s.strip()]:
            self.run(stmt)
        self._ensure_column('sandbox_appointments', 'created_at', 'TEXT')
        self._ensure_column('sandbox_contacts', 'email', 'TEXT')
        self._ensure_column('cases', 'verification', 'TEXT')
        self._ensure_column('trials', 'only_scenarios', 'TEXT')
        self._ensure_column('agencies', 'vapi_secret_enc', 'TEXT')
        self._ensure_column('agencies', 'vapi_api_key_enc', 'TEXT')
        self._immutability_triggers()

    def _ensure_column(self, table, col, typ):
        try:
            self.run(f'SELECT {col} FROM {table} LIMIT 1')
        except Exception:
            self.run(f'ALTER TABLE {table} ADD COLUMN {col} {typ}')

    def _immutability_triggers(self):
        for t in IMMUTABLE:
            if self.kind == 'sqlite':
                for op in ('UPDATE', 'DELETE'):
                    self.run(f"""CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t}
                                 BEGIN SELECT RAISE(ABORT, 'vaada: {t} is append-only'); END""")
            else:
                self.run("""CREATE OR REPLACE FUNCTION vaada_append_only() RETURNS trigger AS $$
                            BEGIN RAISE EXCEPTION 'vaada: % is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""")
                self.run(f'DROP TRIGGER IF EXISTS {t}_append_only ON {t}')
                self.run(f'CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} '
                         f'FOR EACH ROW EXECUTE FUNCTION vaada_append_only()')


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(config.DATABASE_URL)
        _db.init()
    return _db


def set_db(url: str) -> Database:
    """Used by tests and scripts to point at another database."""
    global _db
    if _db is not None and getattr(_db._local, 'conn', None) is not None:
        _db._local.conn.close()
    _db = Database(url)
    _db.init()
    return _db


def slugify(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:60] or 'x'
