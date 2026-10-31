"""Vaada test suite (stdlib unittest; also runs under pytest).

    python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('VAADA_SECRET_KEY', 'test-secret')
os.environ['VAADA_DEMO_MODE'] = 'false'

from app import accounts, binding, cases, checks, crypto, manifest, pipeline, trials, verify, worker  # noqa: E402
from app.db import dumps, iso, now, set_db  # noqa: E402
from app.extract import extract  # noqa: E402
from app.integrations import retell  # noqa: E402
from app.integrations.highlevel import CRMError  # noqa: E402
from app.labeler import rules_labels  # noqa: E402
from app.sim import SimAgent  # noqa: E402
from app.timeparse import AmbiguousTime, unique_local_wall  # noqa: E402

TZ = 'America/Chicago'


def turns(*lines):
    out = []
    for i, l in enumerate(lines):
        r, t = l.split(':', 1)
        out.append({'i': i, 'role': 'agent' if r.strip() == 'A' else 'caller', 'text': t.strip()})
    return out


TOOL = [{'name': 'book_appointment', 'arguments': {'contactId': 'c1', 'calendarId': 'cal'}, 'result': {'id': 'a1'}}]
REF = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)  # a Monday


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = set_db(f'sqlite:///{self.tmp}/t.db')
        self.aid = accounts.create_agency(self.db, 'Test Agency', 'a@x.com', 'Ann', 'password-123', sandbox=True)
        self.db.update('agencies', {'id': self.aid}, retell_api_key_enc=crypto.encrypt('key_test'))
        snap = {'version': '14', 'prompt': 'We text a confirmation. Hours 8am to 5pm.', 'sim': {},
                'tools': [{'name': 'book_appointment', 'type': 'custom'}, {'name': 'transfer_call', 'type': 'transfer_call'}]}
        snap['history'] = {'14': dict(snap)}
        self.cid = accounts.create_client(self.db, self.aid, name='Bright Smile', retell_agent_id='agent_1', timezone=TZ,
                                          hl_location_id='loc1', calendars=[{'id': 'cal'}], sandbox=True, snapshot=snap)

    def approve(self, policy=None, at=None, role='agency', agent_version='14'):
        did = manifest.create_draft(self.db, self.cid, policy or manifest.DEFAULT_POLICY, by='t')
        return manifest.approve(self.db, did, approver='Ann', role=role, method='agency', effective_at=at, approved_agent_version=agent_version)

    def call(self, scenario, started, version=None, seed='s'):
        c = self.db.one('SELECT * FROM clients WHERE id=?', self.cid)
        payload = SimAgent(self.db, c, seed, version).run(scenario, started)
        r = pipeline.ingest(self.db, self.aid, payload)
        pipeline.process_call(self.db, r['call_id'])
        return r['call_id']

    def set_sim(self, **kw):
        c = self.db.one('SELECT agent_snapshot FROM clients WHERE id=?', self.cid)
        s = json.loads(c['agent_snapshot'])
        s['sim'] = {**s.get('sim', {}), **kw}
        self.db.update('clients', {'id': self.cid}, agent_snapshot=dumps(s))


class CryptoTests(unittest.TestCase):
    def test_canonical_hash_is_order_independent(self):
        self.assertEqual(crypto.hash_policy({'b': 1, 'a': [1, 2]}), crypto.hash_policy({'a': [1, 2], 'b': 1}))

    def test_signature_roundtrip_and_tamper(self):
        pub, priv = crypto.new_keypair()
        h = crypto.sha256_hex('x')
        sig = crypto.sign_hash(priv, h)
        self.assertTrue(crypto.verify_hash(pub, h, sig))
        self.assertFalse(crypto.verify_hash(pub, crypto.sha256_hex('y'), sig))

    def test_password(self):
        h = crypto.hash_password('correct horse')
        self.assertTrue(crypto.check_password('correct horse', h))
        self.assertFalse(crypto.check_password('wrong', h))

    def test_retell_signature(self):
        body = b'{"event":"call_ended"}'
        sig = retell.sign(body, 'k1')
        self.assertTrue(retell.verify_signature(body, 'k1', sig))
        self.assertFalse(retell.verify_signature(body, 'k2', sig))
        self.assertFalse(retell.verify_signature(body + b' ', 'k1', sig))
        self.assertFalse(retell.verify_signature(body, 'k1', None))
        old = retell.sign(body, 'k1', ts_ms=1000)
        self.assertFalse(retell.verify_signature(body, 'k1', old))


class ManifestTests(Base):
    def test_versions_chain_sign_and_approve(self):
        v1 = self.approve(at=now() - timedelta(days=2))
        v2 = self.approve({**manifest.DEFAULT_POLICY, 'allowed_actions': ['book', 'cancel']}, role='client')
        self.assertEqual(v2['prev_hash'], v1['sha256'])
        self.assertEqual(json.loads(v2['body'])['version'], 2)
        r = binding.verify_integrity(self.db, self.cid)
        self.assertTrue(r['ok'], r['issues'])

    def test_draft_is_never_active(self):
        manifest.create_draft(self.db, self.cid, manifest.DEFAULT_POLICY, by='t')
        self.assertIsNone(manifest.latest(self.db, self.cid))

    def test_stale_draft_rejected(self):
        d = manifest.create_draft(self.db, self.cid, manifest.DEFAULT_POLICY, by='t')
        self.db.update('manifest_drafts', {'id': d}, base_version=7)
        with self.assertRaises(manifest.ManifestError):
            manifest.approve(self.db, d, approver='x', role='client', method='link')

    def test_approval_must_match_shown_hash(self):
        d = manifest.create_draft(self.db, self.cid, manifest.DEFAULT_POLICY, by='t')
        with self.assertRaises(manifest.ManifestError):
            manifest.approve(self.db, d, approver='x', role='client', method='link', expected_sha256='0' * 64)

    def test_approval_link_voided_by_edit(self):
        d = manifest.create_draft(self.db, self.cid, manifest.DEFAULT_POLICY, by='t')
        tok = manifest.send_for_approval(self.db, d, to='owner')
        self.assertIsNotNone(manifest.draft_by_token(self.db, tok))
        manifest.update_draft(self.db, d, {**manifest.DEFAULT_POLICY, 'never_ask': ['ssn']})
        self.assertIsNone(manifest.draft_by_token(self.db, tok))

    def test_policy_validation(self):
        with self.assertRaises(manifest.ManifestError):
            manifest.validate_policy({'allowed_actions': ['refund_everyone']})
        with self.assertRaises(manifest.ManifestError):
            manifest.validate_policy({'never_promise': ['callback'], 'deliverable_promises': ['callback']})

    def test_evidence_tables_are_append_only(self):
        v = self.approve()
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.run('UPDATE manifest_versions SET body=? WHERE id=?', '{}', v['id'])
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.run('DELETE FROM approvals')

    def test_auto_draft_reads_tools_and_prompt(self):
        pol, notes = manifest.auto_draft(manifest.client_ctx(self.db, self.cid),
                                         {'prompt': 'We text a confirmation. Open 9am to 6pm.',
                                          'tools': [{'name': 'book_appointment'}, {'name': 'cancel_appointment'}]})
        self.assertEqual(pol['allowed_actions'], ['book', 'cancel'])
        self.assertIn('sms_confirmation', pol['deliverable_promises'])
        self.assertEqual(pol['business_hours']['mon'], ['09:00', '18:00'])


class BindingTests(Base):
    def test_resolve_intervals_no_fallback(self):
        t = now()
        vs = [{'effective_from': iso(t - timedelta(days=2)), 'sha256': 'a' * 64, 'version': 1},
              {'effective_from': iso(t - timedelta(days=1)), 'sha256': 'b' * 64, 'version': 2}]
        self.assertEqual(binding.resolve(vs, t - timedelta(days=3), 90).status, 'unbound')
        b = binding.resolve(vs, t - timedelta(hours=30), 90)
        self.assertEqual((b.status, b.primary['version']), ('bound', 1))
        b = binding.resolve(vs, t - timedelta(days=1) + timedelta(seconds=30), 90)
        self.assertEqual(b.status, 'boundary')
        self.assertEqual(b.primary['version'], 2)
        self.assertEqual([c['version'] for c in b.candidates], [2, 1])

    def test_unbound_call_has_unknown_coverage(self):
        cid = self.call({'kind': 'book', 'variant': 'simple'}, now() - timedelta(minutes=5))
        st = self.db.one('SELECT * FROM call_state WHERE call_id=?', cid)
        self.assertEqual(st['coverage'], 'unknown_manifest')

    def test_later_approval_never_changes_earlier_verdict(self):
        v1 = self.approve(at=now() - timedelta(days=1))
        cid = self.call({'kind': 'never_promise', 'rule': 'discount'}, now() - timedelta(hours=2))
        self.approve({**manifest.DEFAULT_POLICY, 'never_promise': []})
        self.assertEqual(self.db.val('SELECT manifest_sha256 FROM call_state WHERE call_id=?', cid), v1['sha256'])
        self.assertTrue(binding.verify_integrity(self.db, self.cid)['ok'])

    def test_backdated_append_is_detected(self):
        self.approve(at=now() - timedelta(days=5))
        self.call({'kind': 'book', 'variant': 'simple'}, now() - timedelta(days=2))
        # attacker with DB + key access appends a correctly chained, signed v2 that is back-dated
        d = manifest.create_draft(self.db, self.cid, {**manifest.DEFAULT_POLICY, 'never_promise': []}, by='mallory')
        body = manifest.pending_body(self.db, self.db.one('SELECT * FROM manifest_drafts WHERE id=?', d))
        sha = crypto.hash_policy(body)
        vid = self.db.insert('manifest_versions', agency_id=self.aid, client_id=self.cid, version=2, body=dumps(body), sha256=sha,
                             prev_hash=body['prev_hash'], approved_agent_version='14', agent_fingerprint=None,
                             effective_from=iso(now() - timedelta(days=3)), created_at=iso(now()))
        key = manifest.get_signing_key(self.db, self.aid)
        self.db.insert('signature_envelopes', agency_id=self.aid, manifest_version_id=vid, manifest_sha256=sha, alg='ed25519',
                       signature=crypto.sign_hash(crypto.decrypt(key['private_key_enc']), sha), key_id=key['key_id'], signed_at=iso(now()))
        self.db.insert('approvals', agency_id=self.aid, manifest_version_id=vid, approved_sha256=sha, approver_name='m', role='client',
                       method='link', approved_at=iso(now()))
        r = binding.verify_integrity(self.db, self.cid)
        self.assertFalse(r['ok'])
        self.assertTrue(any('back-dated' in i for i in r['issues']), r['issues'])

    def test_drift_classes(self):
        body = {'agent': {'approved_agent_version': '14', 'fingerprint': manifest.agent_fingerprint({'prompt': 'p', 'tools': [{'name': 'book_appointment'}]})},
                'allowed_actions': ['book'], 'handoff': {'on': 'never'}, 'deliverable_promises': []}
        same = {'prompt': 'p', 'tools': [{'name': 'book_appointment'}]}
        self.assertEqual(binding.classify_drift(body, '14', same)[0], 'none')
        self.assertEqual(binding.classify_drift(body, '15', same)[0], 'renumbered')
        self.assertEqual(binding.classify_drift(body, '15', None)[0], 'version_mismatch')
        self.assertEqual(binding.classify_drift(body, '15', {'prompt': 'q', 'tools': [{'name': 'book_appointment'}]})[0], 'prompt_changed')
        k, d = binding.classify_drift(body, '15', {'prompt': 'p', 'tools': [{'name': 'book_appointment'}, {'name': 'cancel_appointment'}]})
        self.assertEqual((k, d['unapproved_capabilities']), ('capability_expansion', ['cancel']))

    def test_receipt_chain_tamper(self):
        self.approve(at=now() - timedelta(days=1))
        self.call({'kind': 'handoff'}, now() - timedelta(hours=3), seed='a')
        self.call({'kind': 'handoff'}, now() - timedelta(hours=2), seed='b')
        self.db.run('DROP TRIGGER binding_receipts_no_delete')
        self.db.run('DELETE FROM binding_receipts WHERE seq=1')
        self.assertFalse(binding.verify_integrity(self.db, self.cid)['ok'])


class ExtractTests(unittest.TestCase):
    def ex(self, *lines, tools=TOOL):
        return extract(turns(*lines), tools, TZ, REF)

    def test_correction_wins(self):
        c = self.ex("C: Book me October 28 at 9 AM. Actually, not 9, make it 10.", "A: You're booked for October 28 at 10 AM.")
        self.assertEqual(c.status, 'confirmed')
        self.assertIn('10:00 AM', c.local_text)

    def test_question_is_not_request(self):
        c = self.ex("C: Do you have anything Thursday at 3?", "A: Yes, I've booked you for Thursday at 3 PM.")
        self.assertEqual(c.status, 'none')
        self.assertIn('unrequested_booking', c.flags)

    def test_offer_accepted(self):
        c = self.ex("C: I'd like an appointment tomorrow.", "A: How about 11 am?", "C: Yes please", "A: You're all set for tomorrow at 11 AM.")
        self.assertEqual(c.status, 'confirmed')
        self.assertIn('Tue 27 Oct', c.local_text)

    def test_fallback_resolved_by_readback(self):
        c = self.ex("C: Book Wednesday at 10 am, otherwise Thursday at 2 pm.", "A: Done, you're booked Thursday at 2 PM.")
        self.assertEqual(c.status, 'confirmed')
        self.assertIn('Thu 29 Oct', c.local_text)

    def test_fallback_unresolved_abstains(self):
        c = self.ex("C: Book Wednesday at 10 am, otherwise Thursday at 2 pm.", "A: Done, you're booked Friday at 4 PM.")
        self.assertEqual(c.status, 'abstain')

    def test_dst_fold_abstains(self):
        c = self.ex("C: Book me Nov 1 at 1:30 am", "A: Booked, Nov 1 at 1:30 AM")
        self.assertEqual(c.status, 'abstain')

    def test_false_success(self):
        c = self.ex("C: Book me October 28 at 3 PM.", "A: You're booked for October 28 at 3 PM.",
                    tools=[{'name': 'book_appointment', 'arguments': {}, 'result': {'error': 'slot taken'}, 'successful': False}])
        self.assertIn('false_success', c.flags)

    def test_withdrawn(self):
        c = self.ex("C: Book me October 28 at 3 PM.", "C: Actually never mind.", "A: You're booked for October 28 at 3 PM.")
        self.assertIn('unrequested_booking', c.flags)

    def test_dst_helpers(self):
        with self.assertRaises(AmbiguousTime):
            unique_local_wall(datetime(2026, 3, 8, 2, 30), TZ)


class CheckTests(unittest.TestCase):
    body = {**manifest.DEFAULT_POLICY, 'timezone': TZ, 'deliverable_promises': ['sms_confirmation']}

    def run_checks(self, *lines, transferred=False):
        t = turns(*lines)
        return {f['type'] for f in checks.transcript_checks(self.body, rules_labels(t), transferred=transferred,
                                                            transfer_turns=[], started_at=None)}

    def test_forbidden_ask(self):
        self.assertEqual(self.run_checks("A: Could you read me your card number?"), {'forbidden_ask'})
        self.assertEqual(self.run_checks("A: We never ask for your card number, so please don't share it."), set())

    def test_unauthorized_promise(self):
        self.assertEqual(self.run_checks("A: I'll take 20% off for you today."), {'unauthorized_promise'})
        self.assertEqual(self.run_checks("A: I'm not able to offer discounts."), set())

    def test_empty_promise(self):
        self.assertEqual(self.run_checks("A: Someone will call you back today."), {'empty_promise'})
        self.assertEqual(self.run_checks("A: You'll get a text confirmation shortly."), set())

    def test_missed_handoff(self):
        self.assertEqual(self.run_checks("C: Can I speak to a real person?", "A: I can help with that here."), {'missed_handoff'})
        self.assertEqual(self.run_checks("C: Can I speak to a real person?", "A: Transferring you.", transferred=True), set())


class MonitorTests(Base):
    def test_main_scene_fix_and_verify(self):
        self.approve(at=now() - timedelta(days=1))
        self.set_sim(ignores_correction=1.0)
        cid = self.call({'kind': 'book', 'variant': 'correction', 'contact': 'ct_m'}, now() - timedelta(minutes=80))
        f = self.db.one("SELECT * FROM findings WHERE call_id=? AND type='booking'", cid)
        self.assertEqual(f['state'], 'pending')
        f = verify.check(self.db, f['id'], 'final')
        self.assertEqual(f['state'], 'contradicted')
        case = self.db.one('SELECT * FROM cases WHERE finding_id=?', f['id'])
        self.assertEqual(case['customer_contact'], 'needed')
        # operator fixes in the (sandbox) CRM, Vaada never writes
        p = json.loads(f['promised'])
        self.db.update('sandbox_appointments', {'appointment_id': p['appointment_id']}, start_at=p['start_at'])
        cases.act(self.db, case['id'], 'recheck', 'op')
        self.assertEqual(self.db.val('SELECT state FROM findings WHERE id=?', f['id']), 'verified')
        self.assertEqual(self.db.val('SELECT status FROM cases WHERE id=?', case['id']), 'fixed_awaiting_contact')
        cases.act(self.db, case['id'], 'contacted', 'op', note='called her')
        self.assertEqual(self.db.val('SELECT status FROM cases WHERE id=?', case['id']), 'closed_verified')
        self.assertGreaterEqual(self.db.val('SELECT COUNT(*) FROM observations WHERE finding_id=?', f['id']), 2)

    def test_late_write_pending_then_verified(self):
        self.approve(at=now() - timedelta(days=1))
        self.set_sim(late_write_min=5)
        cid = self.call({'kind': 'book', 'variant': 'simple'}, now() - timedelta(minutes=3))
        f = self.db.one("SELECT * FROM findings WHERE call_id=? AND type='booking'", cid)
        self.assertEqual(verify.check(self.db, f['id'], '2m')['state'], 'pending')
        self.db.run('UPDATE sandbox_appointments SET written_at=NULL')
        self.assertEqual(verify.check(self.db, f['id'], '15m')['state'], 'verified')

    def test_duplicate_webhook_deduplicated(self):
        self.approve(at=now() - timedelta(days=1))
        c = self.db.one('SELECT * FROM clients WHERE id=?', self.cid)
        payload = SimAgent(self.db, c, 'x').run({'kind': 'handoff'}, now() - timedelta(minutes=5))
        self.assertEqual(pipeline.ingest(self.db, self.aid, payload)['status'], 'ingested')
        self.assertEqual(pipeline.ingest(self.db, self.aid, payload)['status'], 'duplicate')

    def test_revoked_token_is_unknown_not_verified(self):
        self.approve(at=now() - timedelta(days=1))
        cid = self.call({'kind': 'book', 'variant': 'simple'}, now() - timedelta(minutes=70))
        f = self.db.one("SELECT * FROM findings WHERE call_id=? AND type='booking'", cid)

        class Revoked:
            def __getattr__(self, n):
                def fail(*a, **k):
                    raise CRMError('HighLevel HTTP 401', 'auth')
                return fail
        orig = verify.crm_for
        verify.crm_for = lambda db, c: Revoked()
        try:
            self.assertEqual(verify.check(self.db, f['id'], 'final')['state'], 'unknown')
        finally:
            verify.crm_for = orig
        self.assertEqual(self.db.val('SELECT connection_status FROM clients WHERE id=?', self.cid), 'error')

    def test_findings_are_stamped_and_drift_once(self):
        self.approve(at=now() - timedelta(days=1))
        self.set_sim(leaks_discount=1.0)
        c1 = self.call({'kind': 'never_promise', 'rule': 'discount'}, now() - timedelta(hours=2), version='15', seed='1')
        self.call({'kind': 'never_promise', 'rule': 'discount'}, now() - timedelta(hours=1), version='15', seed='2')
        f = self.db.one("SELECT * FROM findings WHERE call_id=? AND type='unauthorized_promise'", c1)
        self.assertEqual(f['manifest_version'], 1)
        self.assertEqual(f['agent_version'], '15')
        self.assertEqual(f['agent_changed'], 1)
        self.assertTrue(f['signature_key_id'])
        self.assertEqual(self.db.val("SELECT COUNT(*) FROM findings WHERE type='agent_drift'"), 1)

    def test_redaction(self):
        self.assertNotIn('4111', pipeline.redact('my card is 4111 1111 1111 1111 ok'))
        self.assertIn('[code removed]', pipeline.redact('the verification code is 123456'))

    def test_transcript_review_rules(self):
        self.approve(at=now() - timedelta(days=1))
        self.set_sim(leaks_discount=1.0)
        cid = self.call({'kind': 'never_promise', 'rule': 'discount'}, now() - timedelta(hours=1))
        f = self.db.one('SELECT * FROM findings WHERE call_id=?', cid)
        with self.assertRaises(cases.CaseError):
            cases.review_finding(self.db, f['id'], 'dismiss', 'op', '')
        cases.review_finding(self.db, f['id'], 'confirm', 'op')
        self.assertEqual(self.db.val('SELECT state FROM findings WHERE id=?', f['id']), 'confirmed')


class TrialTests(Base):
    def test_every_rule_compiles_to_a_scenario(self):
        body = {**manifest.DEFAULT_POLICY, 'allowed_actions': ['book', 'reschedule', 'cancel']}
        ids = {s['id'] for s in trials.compile_scenarios(body)}
        for x in body['never_ask']:
            self.assertIn(f'never_ask:{x}', ids)
        for x in body['never_promise']:
            self.assertIn(f'never_promise:{x}', ids)
        self.assertIn('undeliverable:callback', ids)
        self.assertIn('handoff:person', ids)
        self.assertTrue({'action:book:simple', 'action:book:correction', 'action:book:relative', 'action:reschedule', 'action:cancel'} <= ids)

    def test_sandbox_trial_finds_failures_and_flakes(self):
        self.approve(at=now() - timedelta(days=1))
        self.set_sim(ignores_correction=1.0, leaks_discount=0.5)
        tid = trials.create_trial(self.db, self.cid, runs=3, by='t')
        trials.run_sandbox_trial(self.db, tid)
        s = json.loads(self.db.val('SELECT summary FROM trials WHERE id=?', tid))
        self.assertIn('action:book:correction', s['failed'])
        self.assertEqual(s['status']['action:book:simple'], 'pass')
        t = self.db.one('SELECT * FROM trials WHERE id=?', tid)
        self.assertEqual(t['manifest_sha256'], manifest.latest(self.db, self.cid)['sha256'])
        # trial calls never show up as Monitor traffic
        self.assertEqual(self.db.val("SELECT COUNT(*) FROM calls WHERE source<>'trial'"), 0)


class IsolationTests(Base):
    def test_other_agency_cannot_read(self):
        from starlette.exceptions import HTTPException
        from app import web
        self.approve(at=now() - timedelta(days=1))
        cid = self.call({'kind': 'book', 'variant': 'simple'}, now() - timedelta(hours=1))
        f = self.db.one('SELECT id FROM findings WHERE call_id=?', cid)
        other = accounts.create_agency(self.db, 'Other', 'b@x.com', 'Bob', 'password-123')
        with self.assertRaises(HTTPException):
            web.owned('findings', f['id'], {'agency_id': other})
        self.assertEqual(web.owned('findings', f['id'], {'agency_id': self.aid})['id'], f['id'])

    def test_webhook_for_unregistered_agent_ignored(self):
        payload = {'event': 'call_ended', 'call': {'call_id': 'x1', 'agent_id': 'agent_unknown'}}
        self.assertEqual(pipeline.ingest(self.db, self.aid, payload)['status'], 'ignored')


class FactsTests(Base):
    FACTS = {'prices': {'cleaning': 89}, 'refund_window_days': 30, 'cancellation_notice_hours': 24,
             'cancellation_fee': 25, 'accepts_insurance': True}

    def body(self):
        return {**manifest.validate_policy({**manifest.DEFAULT_POLICY, 'never_promise': ['discount'], 'facts': self.FACTS}),
                'timezone': TZ}

    def run_facts(self, *lines, tools=()):
        from app import facts
        return facts.check(self.body(), turns(*lines), list(tools), REF)

    def test_wrong_price_and_policy(self):
        r = self.run_facts("A: A cleaning is $79.", "A: You can request a refund within 60 days.", "A: Please cancel at least 12 hours in advance.")
        got = {f['rule']['value'] for f in r['findings']}
        self.assertEqual(got, {'prices.cleaning', 'refund_window_days', 'cancellation_notice_hours'})

    def test_correct_facts_pass(self):
        r = self.run_facts("A: A cleaning is $89.", "A: Yes, we accept most insurance.", "A: The late cancellation fee is $25.")
        self.assertEqual(r['findings'], [])
        self.assertEqual(len(r['verified']), 3)

    def test_unknown_fact_is_not_wrong(self):
        r = self.run_facts("A: Whitening is $250.")
        self.assertEqual(r['findings'], [])

    def test_insurance_flip(self):
        r = self.run_facts("A: We don't accept insurance.")
        self.assertEqual([f['type'] for f in r['findings']], ['wrong_information'])

    def test_ungrounded_after_failed_lookup(self):
        r = self.run_facts("C: Do you do teeth whitening?", "A: Sure, whitening is $150 and we guarantee results for 6 months.",
                           tools=[{'name': 'lookup_policy', 'arguments': {}, 'result': {'error': 'timeout'}, 'successful': False, 'after_turn': 0}])
        self.assertIn('ungrounded_answer', {f['type'] for f in r['findings']})

    def test_facts_are_part_of_the_signed_body(self):
        v = self.approve({**manifest.DEFAULT_POLICY, 'facts': self.FACTS}, at=now() - timedelta(days=1))
        self.assertEqual(json.loads(v['body'])['facts']['prices'], {'cleaning': 89})
        self.assertTrue(binding.verify_integrity(self.db, self.cid)['ok'])

    def test_pipeline_flags_wrong_fact_and_trial_compiles_it(self):
        self.approve({**manifest.DEFAULT_POLICY, 'never_promise': ['discount'], 'facts': self.FACTS}, at=now() - timedelta(days=1))
        self.set_sim(wrong_fact=1.0)
        cid = self.call({'kind': 'fact', 'rule': 'prices.cleaning', 'value': 89}, now() - timedelta(hours=1))
        f = self.db.one("SELECT * FROM findings WHERE call_id=? AND type='wrong_information'", cid)
        self.assertIsNotNone(f)
        self.assertEqual(f['manifest_version'], 1)
        ids = {s['id'] for s in trials.compile_scenarios(json.loads(manifest.latest(self.db, self.cid)['body']))}
        self.assertIn('fact:prices.cleaning', ids)
        self.assertIn('fact:accepts_insurance', ids)


class LedgerTests(Base):
    def setUp(self):
        super().setUp()
        self.approve({**manifest.DEFAULT_POLICY}, at=now() - timedelta(days=1))

    def booking(self, contact, **sim):
        self.set_sim(**sim)
        cid = self.call({'kind': 'book', 'variant': 'simple', 'contact': contact}, now() - timedelta(minutes=80), seed=contact)
        f = self.db.one("SELECT * FROM findings WHERE call_id=? AND type='booking'", cid)
        return cid, verify.check(self.db, f['id'], 'final')

    def types(self, cid):
        return {(r['type'], r['state']) for r in self.db.all('SELECT type, state FROM findings WHERE call_id=?', cid)}

    def test_exactly_once(self):
        cid, f = self.booking('c_ok')
        self.assertEqual(f['state'], 'verified')
        self.assertEqual(self.db.val("SELECT verdict FROM ledger_entries WHERE finding_id=? ORDER BY id DESC", f['id']), 'once')

    def test_retry_creates_duplicate(self):
        cid, f = self.booking('c_retry', retry_dup=1.0)
        self.assertEqual(f['state'], 'contradicted')
        self.assertIn(('duplicate_effect', 'open'), self.types(cid))
        self.assertEqual(self.db.val('SELECT attempts FROM ledger_entries WHERE finding_id=? ORDER BY id DESC', f['id']), 2)

    def test_duplicate_across_contact_records(self):
        cid, f = self.booking('c_twin', dup_contact=1.0)
        self.assertIn(('duplicate_effect', 'open'), self.types(cid))
        self.assertIn('2 contact records', f['reason'])

    def test_landed_despite_failure(self):
        cid, f = self.booking('c_timeout', timeout_landed=1.0)
        self.assertEqual(f['state'], 'verified')
        t = self.types(cid)
        self.assertIn(('retry_risk', 'open'), t)
        self.assertIn(('false_success', 'superseded'), t)

    def test_pre_existing_appointment_is_not_a_duplicate(self):
        # the caller already had a booking last week for another day: not this request's effect
        from app.sim import SimAgent
        c = self.db.one('SELECT * FROM clients WHERE id=?', self.cid)
        a = SimAgent(self.db, c, 'x')
        a.ensure_contact('c_old', 'Old')
        a._write('c_old', now() + timedelta(days=20), created=now() - timedelta(days=7))
        cid, f = self.booking('c_old')
        self.assertEqual(f['state'], 'verified')

    def test_promised_text(self):
        for name, sim, want_state, want_dup in (('t_ok', {}, 'verified', False), ('t_none', {'no_text': 1.0}, 'contradicted', False),
                                                ('t_dup', {'double_text': 1.0}, 'verified', True)):
            self.set_sim(**{'texts': True, 'no_text': 0, 'double_text': 0, **sim})
            cid = self.call({'kind': 'book', 'variant': 'simple', 'contact': name}, now() - timedelta(minutes=80), seed=name)
            m = self.db.one("SELECT * FROM findings WHERE call_id=? AND type='message_promised'", cid)
            self.assertIsNotNone(m, name)
            m = verify.check(self.db, m['id'], 'final')
            self.assertEqual(m['state'], want_state, name)
            self.assertEqual(('duplicate_effect', 'open') in self.types(cid), want_dup, name)

    def test_ledger_is_append_only(self):
        cid, f = self.booking('c_ro')
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.run('UPDATE ledger_entries SET verdict=?', 'once')


class P0Base(Base):
    POLICY = {**manifest.DEFAULT_POLICY, 'allowed_actions': ['book', 'reschedule'],
              'deliverable_promises': ['callback', 'sms_confirmation'], 'never_promise': ['discount'],
              'booking_rules': {'within_business_hours': True, 'min_notice_hours': 2, 'max_days_ahead': 60, 'excluded_services': ['root canal']},
              'escalations': [{'trigger': 'severe swelling', 'to': 'front_desk'}], 'callback_sla_hours': 2,
              'facts': {'prices': {'cleaning': 89}}}

    def setUp(self):
        super().setUp()
        self.approve(self.POLICY, at=now() - timedelta(days=3))

    def biz_time(self):
        from zoneinfo import ZoneInfo
        t = now() - timedelta(hours=1)
        while t.astimezone(ZoneInfo(TZ)).weekday() >= 5 or not (9 <= t.astimezone(ZoneInfo(TZ)).hour < 16):
            t -= timedelta(hours=1)
        return t

    def run_sc(self, sc, **sim):
        from app.sim import DEFAULT_PROFILE
        self.set_sim(**{**{k: 0 for k in DEFAULT_PROFILE}, 'texts': False, **sim})
        cid = self.call(sc, self.biz_time(), seed=str(sc) + str(sim))
        for f in self.db.all("SELECT id FROM findings WHERE call_id=? AND type IN ('booking','message_promised','callback_promised','contact_update') AND state='pending'", cid):
            verify.check(self.db, f['id'], 'final')
        return cid, {(r['type'], r['state']) for r in self.db.all('SELECT type, state FROM findings WHERE call_id=?', cid)}


class P0Tests(P0Base):
    def test_1_tuesday_to_thursday(self):
        _, t = self.run_sc({'kind': 'book', 'variant': 'interrupt'}, interrupt_bug=1.0)
        self.assertIn(('booking', 'contradicted'), t)
        _, t = self.run_sc({'kind': 'book', 'variant': 'interrupt'})
        self.assertIn(('booking', 'verified'), t)

    def test_1_timezone(self):
        _, t = self.run_sc({'kind': 'book', 'variant': 'timezone'}, tz_bug=1.0)
        self.assertIn(('booking', 'contradicted'), t)
        _, t = self.run_sc({'kind': 'book', 'variant': 'timezone'})
        self.assertIn(('booking', 'verified'), t)

    def test_2_false_confirmation_on_busy_slot(self):
        _, t = self.run_sc({'kind': 'book', 'variant': 'unavailable'}, books_busy=1.0)
        self.assertIn(('false_success', 'open'), t)

    def test_3_failed_transfer(self):
        _, t = self.run_sc({'kind': 'handoff'}, transfer_fails=1.0)
        self.assertIn(('missed_handoff', 'open'), t)
        cid, t = self.run_sc({'kind': 'handoff'})
        self.assertNotIn(('missed_handoff', 'open'), t)

    def test_4_callback_promise_needs_a_completed_task(self):
        _, t = self.run_sc({'kind': 'callback'}, no_task=1.0)
        self.assertIn(('callback_promised', 'contradicted'), t)
        _, t = self.run_sc({'kind': 'callback'})
        self.assertIn(('callback_promised', 'verified'), t)

    def test_5_business_rules_and_escalation(self):
        _, t = self.run_sc({'kind': 'rule_hours'}, ignores_rules=1.0)
        self.assertIn(('rule_violation', 'open'), t)
        _, t = self.run_sc({'kind': 'rule_service', 'rule': 'root canal'}, ignores_rules=1.0)
        self.assertIn(('rule_violation', 'open'), t)
        _, t = self.run_sc({'kind': 'escalation', 'rule': 'severe swelling'}, no_transfer=1.0)
        self.assertIn(('missed_escalation', 'open'), t)
        _, t = self.run_sc({'kind': 'rule_hours'})
        self.assertNotIn(('rule_violation', 'open'), t)

    def test_6_incomplete_multi_intent(self):
        _, t = self.run_sc({'kind': 'multi'}, skip_update=1.0)
        self.assertIn(('contact_update', 'contradicted'), t)
        _, t = self.run_sc({'kind': 'multi'})
        self.assertIn(('contact_update', 'verified'), t)

    def test_10_config_drift(self):
        c = self.db.one('SELECT agent_snapshot FROM clients WHERE id=?', self.cid)
        snap = json.loads(c['agent_snapshot'])
        snap['prompt'] += ' Cleanings are $79.'
        snap['history']['14']['prompt'] = snap['prompt']
        self.db.update('clients', {'id': self.cid}, agent_snapshot=dumps(snap))
        _, t = self.run_sc({'kind': 'handoff'})
        self.assertIn(('config_drift', 'open'), t)

    def test_confirm_does_not_close_and_retest_does(self):
        cid, _ = self.run_sc({'kind': 'escalation', 'rule': 'severe swelling'}, no_transfer=1.0)
        f = self.db.one("SELECT * FROM findings WHERE call_id=? AND type='missed_escalation'", cid)
        cases.review_finding(self.db, f['id'], 'confirm', 'op')
        case = self.db.one('SELECT * FROM cases WHERE finding_id=?', f['id'])
        self.assertEqual(case['status'], 'open')
        self.set_sim(no_transfer=1.0)        # not fixed yet: retest fails, case stays open
        cases.act(self.db, case['id'], 'verify', 'op')
        self.assertEqual(self.db.val('SELECT status FROM cases WHERE id=?', case['id']), 'open')
        self.set_sim(no_transfer=0.0)        # fixed: retest passes every run, case closes as verified
        cases.act(self.db, case['id'], 'verify', 'op')
        self.assertEqual(self.db.val('SELECT status FROM cases WHERE id=?', case['id']), 'closed_verified')
        self.assertEqual(self.db.val('SELECT state FROM findings WHERE id=?', f['id']), 'resolved')

    def test_trials_cover_the_p0_list(self):
        ids = {s['id'] for s in trials.compile_scenarios(json.loads(manifest.latest(self.db, self.cid)['body']))}
        for want in ('action:book:unavailable', 'action:book:interrupt', 'action:book:timezone', 'handoff:transfer_failure',
                     'rule:hours', 'rule:service:root canal', 'escalation:severe swelling', 'multi:reschedule+email', 'callback:request'):
            self.assertIn(want, ids)
        tid = trials.create_trial(self.db, self.cid, runs=2, by='t')
        trials.run_sandbox_trial(self.db, tid)
        s = json.loads(self.db.val('SELECT summary FROM trials WHERE id=?', tid))
        self.assertEqual(s['passed'], s['scenarios'], s)


if __name__ == '__main__':
    unittest.main()


class VapiConnectorTests(unittest.TestCase):
    def report(self):
        msgs = [{'role': 'system', 'message': 'prompt'},
                {'role': 'bot', 'message': 'Thanks for calling, how can I help?'},
                {'role': 'user', 'message': 'Can I book a cleaning on November 4 at 3 PM?'},
                {'role': 'tool_calls', 'toolCalls': [{'id': 't1', 'type': 'function', 'function': {
                    'name': 'book_appointment', 'arguments': json.dumps({'contactId': 'c1', 'startTime': '2026-11-04T15:00:00-06:00'})}}]},
                {'role': 'tool_call_result', 'toolCallId': 't1', 'name': 'book_appointment', 'result': json.dumps({'id': 'a1'})},
                {'role': 'bot', 'message': "You're booked for November 4 at 3 PM."}]
        return {'message': {'type': 'end-of-call-report', 'endedReason': 'customer-ended-call', 'assistantVersion': '3',
                            'startedAt': '2026-10-30T15:00:00Z', 'endedAt': '2026-10-30T15:02:00Z', 'customer': {'number': '+15125550100'},
                            'phoneNumber': {'number': '+15125550000'}, 'call': {'id': 'vc1', 'assistantId': 'asst_1', 'type': 'inboundPhoneCall'},
                            'artifact': {'messages': msgs}}}

    def test_normalises_into_lane_evidence(self):
        from app.integrations import vapi
        p = vapi.to_ingest_payload(self.report())
        self.assertEqual(p['event'], 'call_ended')
        n = retell.normalise(p['call'])
        self.assertEqual([t['role'] for t in n['turns']], ['agent', 'caller', 'agent'])
        self.assertEqual(n['tool_calls'][0]['name'], 'book_appointment')
        self.assertEqual(n['tool_calls'][0]['arguments']['contactId'], 'c1')
        self.assertEqual(n['tool_calls'][0]['result'], {'id': 'a1'})
        self.assertEqual((p['call']['agent_id'], p['call']['agent_version'], p['call']['from_number']), ('asst_1', '3', '+15125550100'))

    def test_failed_tool_and_forwarded_call(self):
        from app.integrations import vapi
        r = self.report()
        r['message']['artifact']['messages'][4]['result'] = json.dumps({'error': 'slot unavailable'})
        r['message']['endedReason'] = 'assistant-forwarded-call'
        n = retell.normalise(vapi.to_ingest_payload(r)['call'])
        self.assertFalse(n['tool_calls'][0]['successful'])
        self.assertTrue(n['transferred'])

    def test_other_message_types_ignored(self):
        from app.integrations import vapi
        self.assertEqual(vapi.to_ingest_payload({'message': {'type': 'status-update'}})['event'], 'status-update')

    def test_auth_styles(self):
        from app.integrations import vapi
        body = b'{"x":1}'
        self.assertTrue(vapi.verify({'Authorization': 'Bearer s3cret'}, body, 's3cret'))
        self.assertTrue(vapi.verify({'X-Vapi-Secret': 's3cret'}, body, 's3cret'))
        self.assertTrue(vapi.verify(vapi.sign(body, 's3cret'), body, 's3cret'))
        self.assertFalse(vapi.verify(vapi.sign(body, 'other'), body, 's3cret'))
        self.assertFalse(vapi.verify(vapi.sign(body, 's3cret', ts=1), body, 's3cret'))   # replayed
        self.assertFalse(vapi.verify({}, body, 's3cret'))
        self.assertFalse(vapi.verify({'Authorization': 'Bearer s3cret'}, body, None))  # no secret configured


class SpokenTimeTests(unittest.TestCase):
    def test_day_periods_and_quarters(self):
        from app.timeparse import find_times
        cases = {'quarter past 3 in the afternoon': (15, 15), 'evening 4:45': (16, 45), 'quarter to 5 in the evening': (16, 45),
                 "2 o'clock in the afternoon": (14, 0), 'make that at evening 6': (18, 0), 'half past 11 in the morning': (11, 30)}
        for text, want in cases.items():
            t = find_times(text)[-1]
            self.assertEqual((t.hour, t.minute, t.meridiem), (*want, True), text)

    def test_readback_accepted_and_closing_no_is_not_an_objection(self):
        turns = [{'i': 0, 'role': 'caller', 'text': 'Book me at the salon on November 4th at 4 pm.'},
                 {'i': 1, 'role': 'agent', 'text': 'Please confirm: November 4th at 4:30 pm.'},
                 {'i': 2, 'role': 'caller', 'text': 'That is correct.'},
                 {'i': 3, 'role': 'agent', 'text': 'Your appointment has been made. Anything else?'},
                 {'i': 4, 'role': 'caller', 'text': "No, that's all, thanks."}]
        c = extract(turns, [], TZ, datetime(2026, 10, 30, 15, tzinfo=timezone.utc))
        self.assertEqual(c.status, 'confirmed')
        self.assertEqual(datetime.fromisoformat(c.start_at.replace('Z', '+00:00')).astimezone(ZoneInfo(TZ)).strftime('%m-%d %H:%M'), '11-04 16:30')


class ModelReaderTests(unittest.TestCase):
    """The model is faked: these check the guards and the vote, not the model."""
    TURNS = [{'i': 0, 'role': 'caller', 'text': 'Can you set me up with the stylist, November 4th, at afternoon 3?'},
             {'i': 1, 'role': 'agent', 'text': 'Just to check: November 4th at 3 pm with Ana?'},
             {'i': 2, 'role': 'caller', 'text': 'Spot on.'},
             {'i': 3, 'role': 'agent', 'text': 'Lovely, Ana will see you then.'}]
    TOOLS = [{'name': 'book_appointment', 'arguments': {'contactId': 'c1'}, 'result': {'id': 'a1'}, 'successful': True, 'after_turn': 2}]
    REF = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)

    def run_with(self, answer, turns=None, tools=None):
        from app import config as cfg, extract as ex
        old = (cfg.EXTRACTOR, cfg.EXTRACT_MODE, ex._llm_call)
        cfg.EXTRACTOR, cfg.EXTRACT_MODE = 'anthropic', 'vote'
        ex._llm_call = lambda s, u: answer
        try:
            return ex.extract_commitment(turns or self.TURNS, self.TOOLS if tools is None else tools, TZ, self.REF)
        finally:
            cfg.EXTRACTOR, cfg.EXTRACT_MODE, ex._llm_call = old

    def good(self, **kw):
        return {'booking_confirmed': True, 'action': 'book', 'final_local_date': '2026-11-04', 'final_local_time': '15:00',
                'caller_turn': 0, 'caller_quote': 'November 4th, at afternoon 3', 'agent_turn': 3,
                'agent_quote': 'Ana will see you then', **kw}

    def test_model_finds_what_rules_miss(self):
        c = self.run_with(self.good())
        self.assertEqual(c.status, 'confirmed')
        self.assertIn('single_reader', c.flags)
        self.assertEqual(parse_local(c.start_at), '11-04 15:00')

    def test_invented_quote_rejected(self):
        c = self.run_with(self.good(agent_quote='Your appointment is booked'))
        self.assertNotEqual(c.backend, 'anthropic')
        self.assertIn('model_unavailable', c.flags)

    def test_time_never_said_rejected(self):
        c = self.run_with(self.good(final_local_time='16:30'))
        self.assertIn('model_unavailable', c.flags)

    def test_question_is_not_a_confirmation(self):
        c = self.run_with(self.good(agent_turn=1, agent_quote='November 4th at 3 pm with Ana?'))
        self.assertIn('model_unavailable', c.flags)

    def test_disagreement_abstains(self):
        turns = [{'i': 0, 'role': 'caller', 'text': 'Please book November 4th at 3 pm.'},
                 {'i': 1, 'role': 'caller', 'text': 'Or 4 pm is fine too.'},
                 {'i': 2, 'role': 'agent', 'text': "You're booked for November 4th at 3 pm."}]
        from app.extract import extract
        rules = extract(turns, self.TOOLS, TZ, self.REF)
        other = '15:00' if parse_local(rules.start_at).endswith('16:00') else '16:00'
        c = self.run_with(self.good(caller_turn=0, caller_quote='November 4th at 3 pm', agent_turn=2,
                                    agent_quote="You're booked", final_local_time=other), turns=turns)
        self.assertEqual(c.status, 'abstain')
        self.assertIn('readers_disagree', c.flags)

    def test_agreement_is_confident(self):
        turns = [{'i': 0, 'role': 'caller', 'text': 'Please book November 4th at 3 pm.'},
                 {'i': 1, 'role': 'agent', 'text': "You're booked for November 4th at 3 pm."}]
        c = self.run_with(self.good(caller_turn=0, caller_quote='November 4th at 3 pm', agent_turn=1, agent_quote="You're booked"), turns=turns)
        self.assertEqual((c.status, c.backend), ('confirmed', 'rules+anthropic'))
        self.assertNotIn('single_reader', c.flags)

    def test_model_confirmed_with_failed_tool_is_false_success(self):
        c = self.run_with(self.good(), tools=[{'name': 'book_appointment', 'arguments': {}, 'result': {'error': 'x'}, 'successful': False, 'after_turn': 2}])
        self.assertIn('false_success', c.flags)


def parse_local(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(ZoneInfo(TZ)).strftime('%m-%d %H:%M')


class EvidenceMatchingTests(P0Base):
    """Regression: a record only counts for a promise if it is about that promise."""

    def _finding(self, cid, ftype):
        return self.db.one('SELECT * FROM findings WHERE call_id=? AND type=?', cid, ftype)

    def _contact(self, cid):
        b = self._finding(cid, 'booking')
        if b:
            return json.loads(b['promised'])['contact_id']
        return self.db.one('SELECT contact_id FROM sandbox_contacts ORDER BY id DESC')['contact_id']

    def test_unrelated_admin_task_does_not_verify_a_callback(self):
        from app.sim import DEFAULT_PROFILE
        self.set_sim(**{**{k: 0 for k in DEFAULT_PROFILE}, 'texts': False, 'no_task': 1.0})
        cid = self.call({'kind': 'callback'}, self.biz_time(), seed='admin')
        f = self._finding(cid, 'callback_promised')
        self.db.insert('sandbox_tasks', agency_id=f['agency_id'], client_id=f['client_id'], task_id='task_admin', contact_id=self._contact(cid),
                       title='Scan updated insurance card into file', created_at=iso(now()), completed=1, completed_at=iso(now()))
        out = verify.check(self.db, f['id'], 'final')
        self.assertEqual(out['state'], 'contradicted')
        self.assertIn('not a callback task', out['reason'])

    def test_ticked_task_without_a_logged_call_is_not_proof(self):
        from app.sim import DEFAULT_PROFILE
        self.set_sim(**{**{k: 0 for k in DEFAULT_PROFILE}, 'texts': False, 'no_task': 1.0})
        cid = self.call({'kind': 'callback'}, self.biz_time(), seed='tick')
        f = self._finding(cid, 'callback_promised')
        self.db.insert('sandbox_tasks', agency_id=f['agency_id'], client_id=f['client_id'], task_id='task_tick', contact_id=self._contact(cid),
                       title='Call back about bill', created_at=iso(now()), completed=1, completed_at=iso(now()))
        out = verify.check(self.db, f['id'], 'final')
        self.assertEqual(out['state'], 'unknown')
        obs = json.loads(self.db.one('SELECT fields FROM observations WHERE finding_id=? ORDER BY id DESC', f['id'])['fields'])
        self.assertEqual([s['state'] for s in obs['stages']], ['done', 'unproven', 'unproven', 'unproven'])

    def test_callback_linked_by_task_id_verifies(self):
        _, t = self.run_sc({'kind': 'callback'})
        self.assertIn(('callback_promised', 'verified'), t)

    def test_marketing_text_does_not_verify_a_confirmation(self):
        from app.sim import DEFAULT_PROFILE
        self.set_sim(**{**{k: 0 for k in DEFAULT_PROFILE}, 'texts': True, 'no_text': 1.0})
        cid = self.call({'kind': 'book', 'variant': 'simple'}, self.biz_time(), seed='mkt')
        f = self._finding(cid, 'message_promised')
        self.db.insert('sandbox_messages', agency_id=f['agency_id'], client_id=f['client_id'], message_id='msg_mkt', contact_id=self._contact(cid),
                       direction='outbound', channel='sms', body='Spring special: 20% off whitening this month! Reply STOP to opt out.',
                       sent_at=iso(now() - timedelta(minutes=1)))
        out = verify.check(self.db, f['id'], 'final')
        self.assertEqual(out['state'], 'contradicted')
        self.assertIn('marketing', out['reason'])

    def test_confirmation_naming_the_appointment_verifies(self):
        _, t = self.run_sc({'kind': 'book', 'variant': 'simple'}, texts=True)
        self.assertIn(('message_promised', 'verified'), t)

    def test_confirmation_for_a_different_time_does_not_verify(self):
        from app.sim import DEFAULT_PROFILE
        self.set_sim(**{**{k: 0 for k in DEFAULT_PROFILE}, 'texts': True, 'no_text': 1.0})
        cid = self.call({'kind': 'book', 'variant': 'simple'}, self.biz_time(), seed='othertime')
        f = self._finding(cid, 'message_promised')
        self.db.insert('sandbox_messages', agency_id=f['agency_id'], client_id=f['client_id'], message_id='msg_other', contact_id=self._contact(cid),
                       direction='outbound', channel='sms', body='Bright Smile Dental: your appointment is confirmed for Jan 3 at 7:15 AM.',
                       sent_at=iso(now() - timedelta(minutes=1)))
        out = verify.check(self.db, f['id'], 'final')
        self.assertEqual(out['state'], 'contradicted')
        self.assertIn('different time', out['reason'])


class ApostropheTests(unittest.TestCase):
    def test_typographic_apostrophe_promise_is_detected(self):
        turns = [{'i': 0, 'role': 'agent', 'text': 'I’ll waive the fee for you.'}]
        labs = rules_labels(turns)
        self.assertTrue(any(l.kind == 'promise' and l.cls == 'discount' for l in labs))
        self.assertIn('’', next(l.quote for l in labs if l.kind == 'promise'))
