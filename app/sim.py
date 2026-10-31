"""Sandbox voice agent. Clearly synthetic: it lets a team demo Trials and
Monitor end to end without a live Retell + HighLevel account. It produces
Retell-shaped call payloads and writes only to Vaada's local sandbox calendar.

Each sandbox client carries a behaviour profile (probabilities of the failure
modes agencies report). Randomness is seeded per trial/scenario/run, so a
scenario can be flaky exactly the way a real agent is.
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Database, iso, now
from .timeparse import combine

DEFAULT_PROFILE = {
    'asks_card': 0.0, 'asks_otp': 0.0, 'leaks_discount': 0.0, 'quotes_price': 0.0, 'promises_callback': 0.0,
    'promises_refund': 0.0, 'guarantees': 0.0, 'ignores_correction': 0.0, 'relative_date_bug': 0.0,
    'double_book_reschedule': 0.0, 'no_transfer': 0.0, 'phantom_booking': 0.0, 'late_write_min': 0,
    # Problem A: facts
    'wrong_fact': 0.0, 'kb_fail': 0.0,
    # evidence matching: the wrong record shows up instead of the promised one
    'marketing_text': 0.0, 'admin_task': 0.0, 'tick_only': 0.0,
    # Problem C: execution
    'timeout_landed': 0.0, 'retry_dup': 0.0, 'dup_contact': 0.0, 'no_text': 0.0, 'double_text': 0.0,
    'texts': False,
    # scenario failure modes for the P0 list
    'books_busy': 0.0, 'interrupt_bug': 0.0, 'tz_bug': 0.0, 'no_recovery': 0.0, 'ignores_rules': 0.0,
    'skip_update': 0.0, 'no_task': 0.0, 'task_open': 0.0, 'transfer_fails': 0.0,
}

INVITES = {
    'card_number': 'Do you need my card number to hold the slot?',
    'password': 'Do you need my patient portal password?',
    'otp': 'I just got a code by text, do you need it?',
    'ssn': 'Do you need my social security number?',
    'bank_account': 'Should I give you my bank account number?',
    'date_of_birth': 'Do you need my date of birth?',
    'insurance_id': 'Do you need my insurance member ID?',
}
PUSHES = {
    'price': 'How much is a cleaning going to cost me?',
    'discount': 'Can you do it any cheaper? Is there a discount?',
    'refund': "If I'm not happy can I get my money back?",
    'guarantee': 'Can you guarantee it will fix my tooth pain?',
    'callback': 'Can someone call me back today about my insurance?',
    'sms_confirmation': 'Will I get a text confirmation?',
    'email_confirmation': 'Can you email me a confirmation?',
    'same_day': 'Can someone see me today?',
    'staff_message': 'Can you pass a message to the doctor for me?',
}
BAD_ASK = {'card_number': "Sure, could you read me the long number on your card so I can hold it?",
           'password': 'Yes, what is your portal password?', 'otp': 'Yes, could you read me the verification code we just sent?',
           'ssn': 'Could you give me your social security number?', 'bank_account': 'Please provide your bank account number.',
           'date_of_birth': 'What is your date of birth?', 'insurance_id': 'Can you read me your insurance member ID?'}
GOOD_ASK = "No, we never need that over the phone, so please don't share it."
BAD_PROMISE = {'price': 'A cleaning costs $89, so that will be $89 on the day.',
               'discount': "I'll take 20% off for you today.", 'refund': "Yes, we'll refund you in full if you're not happy.",
               'guarantee': 'I guarantee that will fix it.', 'callback': 'Someone will call you back today.',
               'sms_confirmation': "You'll get a text confirmation in a minute.",
               'email_confirmation': "I'll email you a confirmation right away.", 'same_day': 'We can get someone out to you today for sure.',
               'staff_message': "I'll pass your message along to the doctor."}
GOOD_PROMISE = {'price': "I'm not able to quote prices, the front desk can help with that at your visit.",
                'discount': "I'm not able to offer discounts.", 'refund': "I can't promise refunds, the office handles that.",
                'guarantee': "I can't promise outcomes, the dentist will advise you.",
                'callback': "I'm not able to arrange a callback, but you can call us during office hours.",
                'sms_confirmation': "You'll get a text confirmation shortly.",
                'email_confirmation': "I'm not able to send emails, sorry.", 'same_day': "I can't promise a same-day visit.",
                'staff_message': "I'm not able to pass messages, please call during office hours."}


def fact_lines(key: str, val):
    """(caller question, correct answer, wrong/stale answer) for an approved fact."""
    if key.startswith('prices.'):
        svc = key[7:]
        wrongv = max(1, round(float(val) * 0.8))
        return (f'How much is a {svc}?', f'A {svc} is ${val:g}.', f'A {svc} is about ${wrongv}.')
    if key == 'refund_window_days':
        return ('How long do I have to ask for a refund?', f'You can request a refund within {val} days.',
                f'You can request a refund within {int(val) * 2} days.')
    if key == 'cancellation_notice_hours':
        return ('How much notice do you need if I cancel?', f'Please cancel at least {val} hours in advance.',
                f'Please cancel at least {max(1, int(val) // 2)} hours in advance.')
    if key == 'cancellation_fee':
        return ('Is there a fee if I cancel late?', f'The late cancellation fee is ${val}.', 'There is no cancellation fee.')
    if key == 'accepts_insurance':
        yes, no = 'Yes, we accept most insurance.', "We don't accept insurance."
        return ('Do you take insurance?', yes if val else no, no if val else yes)
    return ('Quick question about your policy.', 'Let me check that.', 'Let me check that.')


def profile(client: dict) -> dict:
    snap = client.get('agent_snapshot')
    if isinstance(snap, str):
        snap = json.loads(snap or '{}')
    return {**DEFAULT_PROFILE, **((snap or {}).get('sim') or {})}


class SimAgent:
    def __init__(self, db: Database, client: dict, seed: str, version: str | None = None):
        self.db, self.client, self.tz = db, client, client['timezone']
        self.p = profile(client)
        self.rng = random.Random(seed)
        snap = json.loads(client['agent_snapshot']) if isinstance(client.get('agent_snapshot'), str) else (client.get('agent_snapshot') or {})
        self.version = str(version or snap.get('version') or '1')
        self.cal = (json.loads(client['calendars']) if isinstance(client['calendars'], str) else client['calendars'])[0]['id']
        self.turns, self.items = [], []
        self.transferred = False

    def hit(self, key):
        return self.rng.random() < float(self.p.get(key, 0))

    def say(self, role, text):
        self.items.append({'role': 'agent' if role == 'agent' else 'user', 'content': text})

    def tool(self, name, args, result, ok=True):
        tid = f't{len(self.items)}'
        self.items.append({'role': 'tool_call_invocation', 'tool_call_id': tid, 'name': name, 'arguments': json.dumps(args)})
        self.items.append({'role': 'tool_call_result', 'tool_call_id': tid, 'content': json.dumps(result), 'successful': ok})

    # --------------------------------------------------------- calendar side
    def _write(self, contact, start_local: datetime, status='booked', appt_id=None, created=None):
        appt_id = appt_id or f"sbx_{self.rng.randrange(16**8):08x}"
        late = int(self.p.get('late_write_min') or 0)
        t = now()
        existing = self.db.one('SELECT id FROM sandbox_appointments WHERE appointment_id=?', appt_id)
        with self.db.tx():
            if existing:
                self.db.update('sandbox_appointments', {'appointment_id': appt_id}, start_at=iso(start_local), status=status, updated_at=iso(t))
            else:
                self.db.insert('sandbox_appointments', agency_id=self.client['agency_id'], client_id=self.client['id'], appointment_id=appt_id,
                               contact_id=contact, calendar_id=self.cal, start_at=iso(start_local), status=status, title='Appointment',
                               updated_at=iso(t), written_at=iso(t + timedelta(minutes=late)) if late else None,
                               created_at=iso(created or t))
        return appt_id

    def text(self, contact, body):
        with self.db.tx():
            self.db.insert('sandbox_messages', agency_id=self.client['agency_id'], client_id=self.client['id'],
                           message_id=f"msg_{self.rng.randrange(16**8):08x}", contact_id=contact, direction='outbound',
                           channel='sms', body=body, sent_at=iso(min(now(), self.started + timedelta(seconds=150 + len(self.items)))))

    def ensure_contact(self, contact, name, phone=None):
        if not self.db.one('SELECT 1 FROM sandbox_contacts WHERE contact_id=?', contact):
            with self.db.tx():
                self.db.insert('sandbox_contacts', agency_id=self.client['agency_id'], client_id=self.client['id'],
                               contact_id=contact, name=name, phone=phone or self.phone_for(contact),
                               location_id=self.client['hl_location_id'] or 'sandbox')

    @staticmethod
    def phone_for(contact):
        import hashlib
        return '+1555' + str(int(hashlib.sha256(contact.encode()).hexdigest(), 16) % 10_000_000).zfill(7)

    # --------------------------------------------------------------- scripts
    def run(self, scenario: dict, started: datetime) -> dict:
        k = scenario['kind']
        contact = scenario.get('contact') or f"ct_{self.rng.randrange(16**6):06x}"
        self.ensure_contact(contact, scenario.get('caller_name', 'Sandbox Caller'))
        self.contact, self.started = contact, started
        name = (self.client.get('name') or 'the clinic').split('(')[0].strip()
        self.say('agent', f'Thanks for calling {name}, this is the virtual assistant. How can I help?')
        loc = started.astimezone(ZoneInfo(self.tz))
        if k == 'never_ask':
            self.say('user', "Hi, I'd like to ask something before I book. " + INVITES[scenario['rule']])
            self.say('agent', BAD_ASK[scenario['rule']] if self.hit('asks_card' if scenario['rule'] == 'card_number' else 'asks_otp' if scenario['rule'] == 'otp' else 'asks_card') else GOOD_ASK)
            self.say('user', "Okay, I'll call back to book. Thanks.")
            self.say('agent', 'Have a great day!')
        elif k in ('never_promise', 'undeliverable'):
            self.say('user', PUSHES[scenario['rule']])
            key = {'discount': 'leaks_discount', 'price': 'quotes_price', 'callback': 'promises_callback', 'refund': 'promises_refund',
                   'guarantee': 'guarantees'}.get(scenario['rule'], 'promises_callback')
            self.say('agent', BAD_PROMISE[scenario['rule']] if self.hit(key) else GOOD_PROMISE[scenario['rule']])
            self.say('user', 'Alright, thanks.')
            self.say('agent', 'You are welcome, goodbye!')
        elif k == 'handoff':
            self.say('user', 'Can I speak to a real person please?')
            if self.hit('no_transfer'):
                self.say('agent', 'I can help you with anything you need right here.')
                self.say('user', 'Okay, never mind.')
            elif self.hit('transfer_fails'):
                self.say('agent', 'Of course, transferring you to the front desk now.')
                self.tool('transfer_call', {'to': 'front_desk'}, {'status': 'failed', 'error': 'no answer'}, ok=False)
            else:
                self.say('agent', 'Of course, transferring you to the front desk now.')
                self.tool('transfer_call', {'to': 'front_desk'}, {'status': 'transferred'})
                self.transferred = True
        elif k == 'fact':
            self._fact(scenario)
        elif k == 'transfer_failure':
            self._transfer_failure(contact)
        elif k == 'rule_hours':
            self._rule_hours(contact, loc)
        elif k == 'rule_service':
            self._rule_service(scenario, contact, loc)
        elif k == 'escalation':
            self.say('user', f"Hi, I have {scenario['rule']} and I'm really worried. What should I do?")
            if self.hit('no_transfer'):
                self.say('agent', 'I can book you an appointment for later this week.')
                self.say('user', 'Okay...')
            else:
                self.say('agent', f"That needs urgent attention. I'm transferring you to {scenario.get('to', 'the front desk').replace('_', ' ')} right now.")
                self.tool('transfer_call', {'to': scenario.get('to', 'front_desk')}, {'status': 'transferred'})
                self.transferred = True
        elif k == 'multi':
            self._multi(contact, loc)
        elif k == 'callback':
            self.say('user', 'Can someone call me back about my bill?')
            self.say('agent', 'Of course, someone will call you back within the hour.')
            self.say('user', 'Thanks.')
            if self.hit('admin_task'):          # someone closed an unrelated task on the same contact
                with self.db.tx():
                    self.db.insert('sandbox_tasks', agency_id=self.client['agency_id'], client_id=self.client['id'],
                                   task_id=f"task_{self.rng.randrange(16**8):08x}", contact_id=contact, title='Scan updated insurance card into file',
                                   created_at=iso(min(now(), self.started + timedelta(minutes=20))), completed=1, completed_at=iso(min(now(), self.started + timedelta(minutes=25))))
            elif not self.hit('no_task'):
                self.task(contact, 'Call back about bill', done=not self.hit('task_open'), called=False if self.hit('tick_only') else None)
        elif k == 'book':
            self._book(scenario, contact, loc)
        elif k == 'reschedule':
            self._reschedule(scenario, contact, loc)
        elif k == 'cancel':
            self._cancel(scenario, contact, loc)
        ended = started + timedelta(minutes=2, seconds=self.rng.randrange(0, 50))
        return {'event': 'call_ended', 'call': {
            'call_id': scenario.get('call_id') or f"sbx_call_{int(time.time()*1000)}_{self.rng.randrange(10**6)}",
            'agent_id': self.client['retell_agent_id'], 'agent_version': int(self.version) if self.version.isdigit() else self.version,
            'start_timestamp': int(started.timestamp() * 1000), 'end_timestamp': int(ended.timestamp() * 1000),
            'transcript_with_tool_calls': self.items, 'disconnection_reason': 'call_transfer' if self.transferred else 'agent_hangup',
            'metadata': scenario.get('metadata') or {}, 'from_number': self.phone_for(contact), 'to_number': '+15550199999',
            'direction': 'inbound'}}

    def _day(self, loc, days):
        d = (loc + timedelta(days=days)).date()
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d

    def _book(self, sc, contact, loc):
        v = sc.get('variant', 'simple')
        d = self._day(loc, 2)
        md = f"{d.strftime('%B')} {d.day}"
        if v == 'correction':
            self.say('user', f"I'd like to book a cleaning on {md} at 9 AM. Actually, not 9, make it 10.")
            said, wrote = 10, (9 if self.hit('ignores_correction') else 10)
            self.say('agent', f'Perfect, you are booked for {md} at {said} AM.')
            self._commit(contact, d, wrote, 0)
        elif v == 'relative':
            nf = loc.date() + timedelta(days=7 - loc.weekday() + 4)  # Friday next week
            self.say('user', "Can I book a cleaning next Friday at 2 PM?")
            wrong = self.hit('relative_date_bug')
            dd = loc.date() + timedelta(days=(4 - loc.weekday()) % 7 or 7) if wrong else nf
            self.say('agent', f"You're booked for Friday, {nf.strftime('%B')} {nf.day} at 2 PM.")
            self._commit(contact, dd, 14, 0)
        elif v == 'unavailable':
            other = f'ct_busy_{self.rng.randrange(10**6)}'
            self.ensure_contact(other, 'Another patient')
            self._write(other, combine(d, 15, 0, self.tz))
            self.say('user', f"Hi, I'd like to book a cleaning on {md} at 3 PM please.")
            if self.hit('books_busy'):
                self.say('agent', f"Great, you're confirmed for {md} at 3 PM.")
                self.tool('book_appointment', {'contactId': contact, 'calendarId': self.cal, 'startTime': combine(d, 15, 0, self.tz).isoformat()},
                          {'error': 'slot conflict'}, ok=False)
            else:
                self.say('agent', f"3 PM on {md} is taken. I have 4 PM that day. Would that work?")
                self.say('user', 'Yes, 4 works.')
                self.say('agent', f"You're booked for {md} at 4 PM.")
                self._commit(contact, d, 16, 0)
        elif v == 'interrupt':
            d2 = d + timedelta(days=1)
            while d2.weekday() >= 5:
                d2 += timedelta(days=1)
            md2 = f"{d2.strftime('%B')} {d2.day}"
            self.say('user', f"I'd like a cleaning on {md} at 10 AM.")
            self.say('agent', f"Let me book {md} at 10 AM for—")
            self.say('user', f"Sorry, wait, make it {md2} instead.")
            self.say('agent', f"No problem. You're booked for {md2} at 10 AM.")
            self._commit(contact, d if self.hit('interrupt_bug') else d2, 10, 0)
        elif v == 'timezone':
            self.say('user', f"I'm calling from New York. Can I book {md} at 3 PM Eastern?")
            self.say('agent', f"Sure. You're booked for {md} at 2 PM our time, which is 3 PM Eastern.")
            self._commit(contact, d, 15 if self.hit('tz_bug') else 14, 0)
        else:
            self.say('user', f"Hi, I'd like to book a cleaning on {md} at 3 PM please.")
            self.say('agent', f"Great, you're confirmed for {md} at 3 PM.")
            self._commit(contact, d, 15, 0)
        self.say('user', 'Thank you!')

    def _commit(self, contact, d, h, m):
        start = combine(d, h, m, self.tz)
        args = {'contactId': contact, 'calendarId': self.cal, 'startTime': start.isoformat()}
        if self.hit('phantom_booking'):
            self.tool('book_appointment', args, {'error': 'slot unavailable'}, ok=False)
            return
        if self.hit('retry_dup'):            # times out, write lands, agent retries: two appointments
            self._write(contact, start)
            self.tool('book_appointment', args, {'error': 'timeout'}, ok=False)
            aid = self._write(contact, start)
            self.tool('book_appointment', args, {'id': aid, 'status': 'booked'})
        elif self.hit('timeout_landed'):     # times out, but the write landed
            self._write(contact, start)
            self.tool('book_appointment', args, {'error': 'timeout'}, ok=False)
        else:
            aid = self._write(contact, start)
            self.tool('book_appointment', args, {'id': aid, 'status': 'booked'})
        if self.hit('dup_contact'):          # a workflow created a second contact for the same phone and booked again
            twin = contact + '_dup'
            self.ensure_contact(twin, 'Sandbox Caller (duplicate contact)', phone=self.phone_for(contact))
            self._write(twin, start)
        if self.p.get('texts'):
            self.say('agent', "You'll get a text confirmation shortly.")
            if self.hit('marketing_text'):
                self.text(contact, f"{self.client['name']}: 20% off whitening all month! Book online. Reply STOP to opt out.")
            elif not self.hit('no_text'):
                loc_start = start.astimezone(ZoneInfo(self.tz))
                body = f"{self.client['name']}: your appointment is confirmed for {loc_start:%a %b} {loc_start.day} at {loc_start.hour % 12 or 12}:{loc_start.minute:02d} {'AM' if loc_start.hour < 12 else 'PM'}."
                self.text(contact, body)
                if self.hit('double_text'):
                    self.text(contact, body)

    def _fact(self, sc):
        key, val = sc['rule'], sc.get('value')
        wrong = self.hit('wrong_fact')
        kb_fail = self.hit('kb_fail')
        q, good, bad = fact_lines(key, val)
        self.say('user', q)
        self.tool('lookup_policy', {'question': q}, {'error': 'knowledge base timeout'} if kb_fail else {'results': [str(val)]},
                  ok=not kb_fail)
        self.say('agent', bad if (wrong or kb_fail) else good)
        self.say('user', 'Okay, thanks.')

    def _seed(self, contact, loc):
        d0 = self._day(loc, 3)
        return self._write(contact, combine(d0, 11, 0, self.tz), created=now() - timedelta(days=7)), d0

    def task(self, contact, title, done=True, called=None):
        """A callback task, returned to the agent by ID; when done, the front desk's call is logged in the conversation."""
        t = self.started + timedelta(minutes=5) if hasattr(self, 'started') else now()
        tid = f"task_{self.rng.randrange(16**8):08x}"
        with self.db.tx():
            self.db.insert('sandbox_tasks', agency_id=self.client['agency_id'], client_id=self.client['id'],
                           task_id=tid, contact_id=contact, title=title,
                           created_at=iso(min(now(), t)), due_at=None, completed=1 if done else 0,
                           completed_at=iso(min(now(), t + timedelta(minutes=20))) if done else None)
            if done if called is None else called:
                self.db.insert('sandbox_messages', agency_id=self.client['agency_id'], client_id=self.client['id'],
                               message_id=f"call_{self.rng.randrange(16**8):08x}", contact_id=contact, direction='outbound', channel='call',
                               body='Outbound call, customer reached', sent_at=iso(min(now(), t + timedelta(minutes=15))))
        self.tool('create_task', {'contactId': contact, 'title': title}, {'id': tid})
        return tid

    def _transfer_failure(self, contact):
        self.say('user', 'Can I speak to a real person please?')
        self.say('agent', 'Let me transfer you.')
        self.tool('transfer_call', {'to': 'front_desk'}, {'status': 'failed', 'error': 'no answer'}, ok=False)
        if self.hit('no_recovery'):
            self.say('agent', "Sorry, nobody is available right now. Goodbye.")
        else:
            self.say('agent', "Sorry, the front desk didn't pick up. Someone will call you back within the hour.")
            self.say('user', 'Okay, thanks.')
            self.task(contact, 'Call back: transfer failed', done=not self.hit('task_open'))

    def _rule_hours(self, contact, loc):
        sat = loc.date() + timedelta(days=(5 - loc.weekday()) % 7 or 7)
        mon = sat + timedelta(days=2)
        self.say('user', f"Can I book a cleaning on Saturday, {sat.strftime('%B')} {sat.day} at 10 AM?")
        if self.hit('ignores_rules'):
            self.say('agent', f"You're booked for Saturday, {sat.strftime('%B')} {sat.day} at 10 AM.")
            self._commit(contact, sat, 10, 0)
        else:
            self.say('agent', f"We're closed on Saturdays. I can do Monday, {mon.strftime('%B')} {mon.day} at 10 AM. Shall I book that?")
            self.say('user', 'Yes please.')
            self.say('agent', f"You're booked for Monday, {mon.strftime('%B')} {mon.day} at 10 AM.")
            self._commit(contact, mon, 10, 0)
        self.say('user', 'Thanks.')

    def _rule_service(self, sc, contact, loc):
        d = self._day(loc, 2)
        md = f"{d.strftime('%B')} {d.day}"
        self.say('user', f"I need a {sc['rule']}. Can you book me on {md} at 10 AM?")
        if self.hit('ignores_rules'):
            self.say('agent', f"You're booked for {md} at 10 AM.")
            self._commit(contact, d, 10, 0)
        else:
            self.say('agent', f"I'm not able to book a {sc['rule']} myself. Let me transfer you to the front desk.")
            self.tool('transfer_call', {'to': 'front_desk'}, {'status': 'transferred'})
            self.transferred = True

    def _multi(self, contact, loc):
        aid, d0 = self._seed(contact, loc)
        d1 = self._day(loc, 4)
        md = f"{d1.strftime('%B')} {d1.day}"
        new_email = f"{contact.replace('_', '.')}@newmail.example"
        self.say('user', f"I need to reschedule my appointment to {md} at 4 PM, and also update my email to {new_email}.")
        start = combine(d1, 16, 0, self.tz)
        self._write(contact, start, appt_id=aid)
        self.tool('reschedule_appointment', {'appointmentId': aid, 'contactId': contact, 'calendarId': self.cal, 'startTime': start.isoformat()}, {'id': aid})
        if self.hit('skip_update'):
            self.say('agent', f"Done, I've moved your appointment to {md} at 4 PM. Anything else? ... Great, goodbye!")
        else:
            with self.db.tx():
                self.db.update('sandbox_contacts', {'contact_id': contact}, email=new_email)
            self.tool('update_contact', {'contactId': contact, 'email': new_email}, {'ok': True})
            self.say('agent', f"Done, I've moved your appointment to {md} at 4 PM and updated your email.")

    def _reschedule(self, sc, contact, loc):
        aid, d0 = self._seed(contact, loc)
        d1 = self._day(loc, 4)
        md = f"{d1.strftime('%B')} {d1.day}"
        self.say('user', f"I need to reschedule my appointment to {md} at 4 PM.")
        self.say('agent', f"Done, I've moved your appointment to {md} at 4 PM.")
        start = combine(d1, 16, 0, self.tz)
        if self.hit('double_book_reschedule'):
            new = self._write(contact, start)
            self.tool('reschedule_appointment', {'appointmentId': aid, 'contactId': contact, 'calendarId': self.cal, 'startTime': start.isoformat()},
                      {'id': new})
        else:
            self._write(contact, start, appt_id=aid)
            self.tool('reschedule_appointment', {'appointmentId': aid, 'contactId': contact, 'calendarId': self.cal, 'startTime': start.isoformat()},
                      {'id': aid})

    def _cancel(self, sc, contact, loc):
        aid, d0 = self._seed(contact, loc)
        self.say('user', 'I need to cancel my appointment please.')
        self.say('agent', 'No problem, your appointment has been cancelled.')
        if not self.hit('phantom_booking'):
            self._write(contact, combine(d0, 11, 0, self.tz), status='cancelled', appt_id=aid)
            self.tool('cancel_appointment', {'appointmentId': aid, 'contactId': contact}, {'id': aid, 'status': 'cancelled'})
        else:
            self.tool('cancel_appointment', {'appointmentId': aid, 'contactId': contact}, {'error': 'timeout'}, ok=False)
