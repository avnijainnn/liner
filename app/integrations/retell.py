"""Retell: webhook verification, payload normalisation, read-only agent fetch,
and outbound trial calls from a Vaada-owned caller agent."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

from .. import http as httpx

from .. import config


def verify_signature(body: bytes, api_key: str, signature: str | None, *, max_skew_ms: int = 5 * 60 * 1000) -> bool:
    """Mirror of retell-sdk `Retell.verify`: header "v=<ms>,d=<hex>",
    digest = HMAC-SHA256(api_key, body + str(ms)). Uses the SDK when installed."""
    if not api_key or not signature:
        return False
    try:  # prefer the official implementation when available
        from retell import Retell  # type: ignore
        return bool(Retell(api_key=api_key).verify(body.decode('utf-8'), api_key, signature))
    except ImportError:
        pass
    except Exception:
        return False
    m = re.fullmatch(r'v=(\d+),d=([0-9a-f]+)', signature.strip())
    if not m:
        return False
    ts = int(m.group(1))
    if abs(time.time() * 1000 - ts) > max_skew_ms:
        return False
    digest = hmac.new(api_key.encode(), body + str(ts).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, m.group(2))


def sign(body: bytes, api_key: str, ts_ms: int | None = None) -> str:
    """Produce a Retell-format signature (used by the sandbox and tests)."""
    ts = ts_ms or int(time.time() * 1000)
    return f"v={ts},d={hmac.new(api_key.encode(), body + str(ts).encode(), hashlib.sha256).hexdigest()}"


def _maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def normalise(call: dict) -> dict:
    """Turn a Retell call object into Vaada's evidence shape."""
    turns, tools, pending = [], [], {}
    items = call.get('transcript_with_tool_calls') or call.get('transcript_object') or []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        role = it.get('role')
        if role in ('agent', 'user'):
            txt = (it.get('content') or '').strip()
            if txt:
                turns.append({'i': len(turns), 'role': 'agent' if role == 'agent' else 'caller', 'text': txt})
        elif role == 'tool_call_invocation':
            t = {'id': it.get('tool_call_id'), 'name': it.get('name') or '', 'arguments': _maybe_json(it.get('arguments')) or {},
                 'result': None, 'after_turn': len(turns) - 1}
            tools.append(t)
            pending[t['id']] = t
        elif role == 'tool_call_result':
            t = pending.get(it.get('tool_call_id'))
            if t is not None:
                t['result'] = _maybe_json(it.get('content'))
                t['successful'] = it.get('successful', True)
    if not turns and isinstance(call.get('transcript'), str):
        for line in call['transcript'].splitlines():
            m = re.match(r'\s*(agent|assistant|user|caller|customer)\s*:\s*(.*)', line, re.I)
            if m and m.group(2).strip():
                r = m.group(1).lower()
                turns.append({'i': len(turns), 'role': 'agent' if r in ('agent', 'assistant') else 'caller', 'text': m.group(2).strip()})
    def _ok(t):
        r = t.get('result')
        bad = t.get('successful') is False or (isinstance(r, dict) and (r.get('error') or str(r.get('status', '')).lower() in ('failed', 'no_answer', 'busy')))
        bad = bad or (isinstance(r, str) and re.search(r'fail|no answer|busy|unavailable|error', r, re.I))
        return not bad
    transfers = [t for t in tools if 'transfer' in (t['name'] or '').lower()]
    disc = str(call.get('disconnection_reason') or '').lower()
    transfer = any(_ok(t) for t in transfers) or disc in ('call_transfer', 'transfer', 'transferred')
    transfer_failed = bool(transfers) and not transfer
    transcript = '\n'.join(f"{'Agent' if t['role'] == 'agent' else 'Caller'}: {t['text']}" for t in turns)
    return {'turns': turns, 'tool_calls': tools, 'transcript': transcript, 'transferred': transfer, 'transfer_failed': transfer_failed,
            'disconnection_reason': call.get('disconnection_reason'), 'metadata': call.get('metadata') or {},
            'from_number': call.get('from_number'), 'to_number': call.get('to_number')}


class RetellAPI:
    def __init__(self, api_key: str):
        if not api_key:
            raise RuntimeError('Retell API key not configured')
        self.h = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    def _get(self, path):
        r = httpx.get(config.RETELL_BASE + path, headers=self.h, timeout=15)
        r.raise_for_status()
        return r.json()

    def agent_snapshot(self, agent_id: str, version: str | int | None = None) -> dict:
        """Read the agent's prompt and tools for a given version (read-only)."""
        q = f'?version={version}' if version is not None else ''
        agent = self._get(f'/get-agent/{agent_id}{q}')
        engine = agent.get('response_engine') or {}
        prompt, tools = '', []
        if engine.get('type') == 'retell-llm' and engine.get('llm_id'):
            v = engine.get('version')
            llm = self._get(f"/get-retell-llm/{engine['llm_id']}" + (f'?version={v}' if v is not None else ''))
            prompt = llm.get('general_prompt') or ''
            tools = list(llm.get('general_tools') or [])
            for st in llm.get('states') or []:
                prompt += '\n' + (st.get('state_prompt') or '')
                tools += st.get('tools') or []
        return {'agent_id': agent_id, 'version': str(agent.get('version', version)), 'name': agent.get('agent_name'),
                'prompt': prompt, 'tools': [{'name': t.get('name'), 'type': t.get('type'),
                                             'parameters': t.get('parameters') or {}} for t in tools]}

    def place_trial_call(self, *, from_number: str, to_number: str, caller_agent_id: str, script: str, ref: str) -> str:
        body = {'from_number': from_number, 'to_number': to_number, 'override_agent_id': caller_agent_id,
                'retell_llm_dynamic_variables': {'vaada_script': script}, 'metadata': {'vaada_trial_result_ref': ref}}
        r = httpx.post(config.RETELL_BASE + '/v2/create-phone-call', headers=self.h, json=body, timeout=20)
        r.raise_for_status()
        return r.json().get('call_id')
