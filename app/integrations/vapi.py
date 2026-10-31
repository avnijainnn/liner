"""Vapi: server-URL authentication, end-of-call-report normalisation and a read-only assistant fetch.

Vapi posts `{"message": {"type": "end-of-call-report", ...}}` to the server URL configured on the
assistant, phone number or org. Field names follow the official server SDK (VapiAI/server-sdk-python):

  message.call.id, message.call.assistantId, message.assistantVersion (or call.assistantVersion)
  message.startedAt / endedAt, message.endedReason, message.customer.number, message.phoneNumber.number
  message.artifact.messages[]: {role, message, time, secondsFromStart}
      role user                  -> caller turn
      role bot | assistant       -> agent turn
      role tool_calls            -> toolCalls[{id, type, function:{name, arguments(JSON string)}}]
      role tool_call_result      -> {toolCallId, name, result(string)}
      role system                -> ignored (the prompt)

Vaada turns this into the same Retell-shaped call object the rest of the pipeline already ingests, so
every check (binding, drift, extraction, record reads, ledger) runs unchanged.

Authentication (Vapi "Custom Credential" on the server URL):
  bearer   Authorization: Bearer <secret>
  legacy   X-Vapi-Secret: <secret>
  hmac     x-signature: hex(HMAC-SHA256(secret, [timestamp + "."] + body)), optional x-timestamp
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from .. import config
from .. import http as httpx

TRANSFER_REASONS = {'assistant-forwarded-call', 'assistant-request-returned-forwarding-phone-number'}
TRANSFER_TOOLS = ('transfercall', 'transfer_call', 'transfer')


def verify(headers: dict, body: bytes, secret: str | None, *, max_skew_s: int = 300) -> bool:
    """Accept any of the three credential styles Vapi can send; reject when no secret is configured."""
    if not secret:
        return False
    h = {k.lower(): v for k, v in headers.items()}
    auth = h.get('authorization', '')
    if auth.lower().startswith('bearer ') and hmac.compare_digest(auth[7:].strip(), secret):
        return True
    if 'x-vapi-secret' in h and hmac.compare_digest(h['x-vapi-secret'].strip(), secret):
        return True
    sig = h.get('x-signature')
    if sig:
        ts = h.get('x-timestamp')
        if ts:
            try:
                t = float(ts)
                t = t / 1000 if t > 1e11 else t
            except ValueError:
                return False
            if abs(time.time() - t) > max_skew_s:
                return False
            msg = ts.encode() + b'.' + body
        else:
            msg = body
        want = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, sig.strip().removeprefix('sha256='))
    return False


def sign(body: bytes, secret: str, ts: int | None = None) -> dict:
    """HMAC headers in the format verify() accepts (used by tests and the replay harness)."""
    ts = ts or int(time.time())
    return {'x-timestamp': str(ts), 'x-signature': hmac.new(secret.encode(), str(ts).encode() + b'.' + body, hashlib.sha256).hexdigest()}


def _args(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {'raw': v}
    return v or {}


def _result(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def to_retell_call(message: dict) -> dict:
    """Map a Vapi end-of-call-report message to the Retell call shape Vaada's ingest understands."""
    call = message.get('call') or {}
    art = message.get('artifact') or {}
    items = []
    for m in art.get('messages') or message.get('messages') or []:
        if not isinstance(m, dict):
            continue
        role = (m.get('role') or '').lower()
        if role in ('user', 'customer'):
            items.append({'role': 'user', 'content': m.get('message') or m.get('content') or ''})
        elif role in ('bot', 'assistant'):
            if m.get('toolCalls') or m.get('tool_calls'):      # OpenAI-formatted assistant message carrying tool calls
                role = 'tool_calls'
            else:
                items.append({'role': 'agent', 'content': m.get('message') or m.get('content') or ''})
                continue
        if role == 'tool_calls':
            for tc in m.get('toolCalls') or m.get('tool_calls') or []:
                fn = tc.get('function') or {}
                items.append({'role': 'tool_call_invocation', 'tool_call_id': tc.get('id'), 'name': fn.get('name') or tc.get('name') or '',
                              'arguments': _args(fn.get('arguments'))})
        elif role in ('tool_call_result', 'tool'):
            res = _result(m.get('result') if 'result' in m else m.get('content'))
            failed = isinstance(res, dict) and bool(res.get('error')) or isinstance(res, str) and res.lower().startswith(('error', 'failed'))
            items.append({'role': 'tool_call_result', 'tool_call_id': m.get('toolCallId') or m.get('tool_call_id'),
                          'content': res, 'successful': not failed})
    ended = message.get('endedReason') or call.get('endedReason') or ''
    tool_names = [i.get('name', '').lower() for i in items if i['role'] == 'tool_call_invocation']
    transferred_by_reason = ended in TRANSFER_REASONS
    customer = message.get('customer') or call.get('customer') or {}
    phone = message.get('phoneNumber') or call.get('phoneNumber') or {}
    version = message.get('assistantVersion') or call.get('assistantVersion') or (call.get('metadata') or {}).get('agent_version')
    return {
        'call_id': call.get('id') or message.get('callId'),
        'agent_id': call.get('assistantId') or (message.get('assistant') or {}).get('id'),
        'agent_version': str(version) if version is not None else 'latest',
        'start_timestamp': message.get('startedAt') or call.get('startedAt'),
        'end_timestamp': message.get('endedAt') or call.get('endedAt'),
        'transcript_with_tool_calls': items,
        'transcript': art.get('transcript') or message.get('transcript'),
        'disconnection_reason': 'call_transfer' if transferred_by_reason and not any(n in TRANSFER_TOOLS for n in tool_names) else ended,
        'metadata': call.get('metadata') or {},
        'from_number': customer.get('number') if call.get('type') != 'outboundPhoneCall' else phone.get('number'),
        'to_number': phone.get('number') if call.get('type') != 'outboundPhoneCall' else customer.get('number'),
        'direction': 'outbound' if call.get('type') == 'outboundPhoneCall' else 'inbound',
    }


def to_ingest_payload(payload: dict) -> dict | None:
    """Wrap as Vaada's ingest payload, or None for message types Vaada does not act on."""
    message = payload.get('message') if isinstance(payload.get('message'), dict) else payload
    if (message.get('type') or '') != 'end-of-call-report':
        return {'event': message.get('type') or 'unknown', 'call': {}}
    return {'event': 'call_ended', 'call': to_retell_call(message)}


class VapiAPI:
    """Read-only: assistant prompt + tools, for drift checks and manifest auto-drafts."""

    def __init__(self, api_key: str):
        if not api_key:
            raise RuntimeError('Vapi API key not configured')
        self.h = {'Authorization': f'Bearer {api_key}'}

    def _get(self, path):
        r = httpx.get(config.VAPI_BASE + path, headers=self.h, timeout=15)
        r.raise_for_status()
        return r.json()

    def agent_snapshot(self, assistant_id: str, version: str | None = None) -> dict:
        a = self._get(f'/assistant/{assistant_id}')
        model = a.get('model') or {}
        prompt = '\n'.join(m.get('content') or '' for m in model.get('messages') or [] if m.get('role') == 'system')
        tools = [{'name': (t.get('function') or {}).get('name') or t.get('type'), 'type': t.get('type'),
                  'parameters': (t.get('function') or {}).get('parameters') or {}} for t in model.get('tools') or []]
        for tid in model.get('toolIds') or []:
            try:
                t = self._get(f'/tool/{tid}')
                tools.append({'name': (t.get('function') or {}).get('name') or t.get('type'), 'type': t.get('type'),
                              'parameters': (t.get('function') or {}).get('parameters') or {}})
            except Exception:
                tools.append({'name': tid, 'type': 'unknown', 'parameters': {}})
        return {'agent_id': assistant_id, 'version': str(version or a.get('updatedAt') or 'latest'), 'name': a.get('name'),
                'prompt': prompt, 'tools': tools}
