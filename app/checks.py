"""Deterministic manifest rules. Labels come from labeler.py; *this* module
decides whether a label breaks the manifest in force. Transcript findings are
flag-for-review only."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .labeler import Label
from .manifest import ASK_CLASSES, DELIVERABLE_TYPES, PROMISE_CLASSES

SEV = {'missed_escalation': 'high', 'rule_violation': 'high', 'incomplete_request': 'high', 'wrong_information': 'high', 'ungrounded_answer': 'high', 'forbidden_ask': 'high', 'unauthorized_promise': 'high', 'empty_promise': 'medium', 'missed_handoff': 'medium',
       'false_success': 'high', 'unrequested_booking': 'high', 'readback_mismatch': 'medium'}

TITLES = {
    'forbidden_ask': 'Forbidden ask', 'unauthorized_promise': 'Unauthorized promise', 'empty_promise': 'Empty promise',
    'missed_handoff': 'Missed handoff', 'booking': 'Booking check', 'agent_drift': 'Agent changed since approval',
    'false_success': 'False success', 'unrequested_booking': 'Unrequested booking', 'action_not_allowed': 'Action outside manifest',
    'coverage': 'Coverage incident', 'wrong_information': 'Wrong information', 'ungrounded_answer': 'Ungrounded answer',
    'message_promised': 'Promised text', 'callback_promised': 'Promised callback', 'contact_update': 'Requested contact update',
    'missed_escalation': 'Missed escalation', 'rule_violation': 'Business-rule violation', 'incomplete_request': 'Incomplete request',
    'config_drift': 'Live agent out of sync with rules', 'duplicate_effect': 'Duplicate side effect', 'retry_risk': 'Write landed despite failure',
}
# The three problems Vaada covers: what the agent said, what happened, how it was executed.
PROBLEM = {'wrong_information': 'semantic', 'ungrounded_answer': 'semantic', 'unauthorized_promise': 'semantic',
           'empty_promise': 'semantic', 'forbidden_ask': 'semantic',
           'booking': 'outcome', 'message_promised': 'outcome', 'false_success': 'outcome', 'unrequested_booking': 'outcome',
           'missed_handoff': 'outcome', 'action_not_allowed': 'outcome', 'callback_promised': 'outcome', 'contact_update': 'outcome',
           'missed_escalation': 'outcome', 'incomplete_request': 'outcome', 'rule_violation': 'semantic', 'config_drift': 'deployment',
           'duplicate_effect': 'execution', 'retry_risk': 'execution', 'agent_drift': 'deployment'}
RECORD_TYPES = ('booking', 'message_promised', 'callback_promised', 'contact_update')


def booking_rule_checks(body: dict, start_at: datetime | None, call_start: datetime | None, turns: list[dict]) -> list[dict]:
    """P0 #5: the booking the agent made must respect the client's business rules."""
    br = body.get('booking_rules') or {}
    out = []
    if start_at is None:
        return out
    tz = ZoneInfo(body.get('timezone') or 'UTC')
    loc = start_at.astimezone(tz)
    if br.get('within_business_hours') and not within_hours(body, loc):
        out.append({'rule': {'field': 'booking_rules.within_business_hours', 'value': 'hours', 'text': 'Bookings only inside business hours'},
                    'reason': f"Booked {loc.strftime('%a %-I:%M %p')}, outside approved business hours"})
    if br.get('min_notice_hours') and call_start and (start_at - call_start).total_seconds() < br['min_notice_hours'] * 3600:
        out.append({'rule': {'field': 'booking_rules.min_notice_hours', 'value': 'notice', 'text': f"At least {br['min_notice_hours']} hours notice"},
                    'reason': f"Booked with less than {br['min_notice_hours']} hours notice"})
    if br.get('max_days_ahead') and call_start and (start_at - call_start).days > br['max_days_ahead']:
        out.append({'rule': {'field': 'booking_rules.max_days_ahead', 'value': 'horizon', 'text': f"No more than {br['max_days_ahead']} days ahead"},
                    'reason': f"Booked more than {br['max_days_ahead']} days ahead"})
    caller = ' '.join(t['text'].lower() for t in turns if t['role'] == 'caller')
    for svc in br.get('excluded_services') or []:
        if svc in caller:
            out.append({'rule': {'field': 'booking_rules.excluded_services', 'value': svc, 'text': f'Never book {svc}'},
                        'reason': f'Caller asked about {svc}, which this business does not let the agent book, and the agent booked anyway'})
    for f in out:
        f.update(type='rule_violation', severity='high', span={})
    return out


def within_hours(body: dict, at_local: datetime) -> bool:
    day = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'][at_local.weekday()]
    win = (body.get('business_hours') or {}).get(day)
    if not win:
        return False
    hm = at_local.strftime('%H:%M')
    return win[0] <= hm < win[1]


def transcript_checks(body: dict, labels: list[Label], *, transferred: bool, transfer_turns: list[int],
                      started_at: datetime | None, transfer_failed: bool = False, turns: list[dict] | None = None) -> list[dict]:
    out = []
    never_ask = set(body.get('never_ask', []))
    never_promise = set(body.get('never_promise', []))
    deliverable = set(body.get('deliverable_promises', []))
    for l in labels:
        span = {'turn': l.turn, 'start': l.start, 'end': l.end, 'quote': l.quote, 'labeler': l.backend}
        if l.kind == 'ask' and l.cls in never_ask:
            out.append({'type': 'forbidden_ask', 'severity': SEV['forbidden_ask'], 'span': span,
                        'rule': {'field': 'never_ask', 'value': l.cls, 'text': ASK_CLASSES[l.cls]},
                        'reason': f'Agent asked for {ASK_CLASSES[l.cls].lower()}, which the manifest says never to ask for'})
        elif l.kind == 'promise' and l.cls in never_promise:
            out.append({'type': 'unauthorized_promise', 'severity': SEV['unauthorized_promise'], 'span': span,
                        'rule': {'field': 'never_promise', 'value': l.cls, 'text': PROMISE_CLASSES[l.cls]},
                        'reason': f'Agent promised {PROMISE_CLASSES[l.cls].lower()}, which the manifest never allows'})
        elif l.kind == 'promise' and l.cls in DELIVERABLE_TYPES and l.cls not in deliverable:
            out.append({'type': 'empty_promise', 'severity': SEV['empty_promise'], 'span': span,
                        'rule': {'field': 'deliverable_promises', 'value': l.cls, 'text': PROMISE_CLASSES[l.cls]},
                        'reason': f'Agent promised {PROMISE_CLASSES[l.cls].lower()}, but this setup cannot deliver it'})
    h = body.get('handoff') or {}
    reqs = [l for l in labels if l.kind == 'handoff_request']
    if reqs and h.get('on') in ('caller_requests_person', 'always_on_request'):
        first = reqs[0]
        applies = True
        if h.get('hours') == 'business' and started_at is not None:
            applies = within_hours(body, started_at.astimezone(ZoneInfo(body.get('timezone') or 'UTC')))
        did_transfer = transferred and (not transfer_turns or any(t >= first.turn for t in transfer_turns))
        if applies and not did_transfer:
            out.append({'type': 'missed_handoff', 'severity': SEV['missed_handoff'],
                        'span': {'turn': first.turn, 'start': first.start, 'end': first.end, 'quote': first.quote, 'labeler': 'rules'},
                        'rule': {'field': 'handoff', 'value': h, 'text': f"Transfer to {h.get('to', 'front desk').replace('_', ' ')} when the caller asks for a person"},
                        'reason': ('Caller asked for a person; a transfer was attempted but failed' if transfer_failed
                                   else 'Caller asked for a person during the handoff window and no transfer happened')})
    # escalation triggers: must reach a person at any hour
    for e in body.get('escalations') or []:
        hit = next((t for t in (turns or []) if t['role'] == 'caller' and e['trigger'] in t['text'].lower()), None)
        if hit and not transferred:
            out.append({'type': 'missed_escalation', 'severity': 'high',
                        'span': {'turn': hit['i'], 'start': 0, 'end': len(hit['text']), 'quote': hit['text'], 'labeler': 'rules'},
                        'rule': {'field': 'escalations', 'value': e['trigger'], 'text': f"\"{e['trigger']}\" must be transferred to {e['to'].replace('_', ' ')} immediately"},
                        'reason': f"Caller said \"{e['trigger']}\" and the call was {'not successfully ' if transfer_failed else 'never '}transferred"})
    # one finding per (type, class) per call
    seen, uniq = set(), []
    for f in out:
        k = (f['type'], f['rule']['value'] if not isinstance(f['rule']['value'], dict) else 'handoff')
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def finding_signature(findings: list[dict]) -> set:
    return {(f['type'], str(f['rule'].get('value'))) for f in findings}
