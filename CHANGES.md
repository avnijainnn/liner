# What was missing or broken in lane-main, and what changed

## Bugs and spec violations in the previous code
1. **Two divergent codebases.** `app/` and `demo_verified/app/` had different logic, plus `.bak` files and about 15 one-off Windows patch notes. These are replaced by a single `app/`.
2. **The manifest fell back to unapproved versions.** `manifest_for_start` returned *any* manifest for demo clients, which breaks spec rule 5. Lane now resolves only approved versions and never uses "latest".
3. **The chain was never verified.** `prev_hash` was stored but nothing walked it, and there was no Ed25519 envelope and no integrity check. All three now exist, and a back-dated append is detected too.
4. **Drift was a string comparison** that opened one finding per call. It is now a capability-surface classification with one finding per client per version pair.
5. **Only 1 of the 4 transcript checks existed.** Unauthorized promise ran only with an OpenAI key and was skipped for demo clients. All four checks now run offline, with optional LLM labels.
6. **Booking extraction handled one explicit date + AM/PM.** It now covers corrections, fallbacks, offers accepted, questions ≠ requests, relative dates, the inherited meridiem, reschedule/cancel, and abstains on DST gaps and folds.
7. **Cancelled recheck jobs by mutating `db.new`** inside the pipeline, which was fragile. Jobs are now explicit rows with locking, retries and backoff.
8. **Read timing ignored near appointments.** Reads now compress when the appointment is close, as the spec says ("sooner for appointments that are close").
9. **States were missing.** superseded, waived and fixed-awaiting-customer-contact did not exist.
10. **Trials only judged hand-written synthetic samples.** There were no runs per scenario, no flaky detection, no diff against the last trial and no sharing.
11. **Approval was operator-entered.** There was no client approval link and no exact-hash confirmation. Editing a draft did not void an approval request.
12. **Auth was one Basic-auth user** with no tenancy and CSRF only via the Origin header, which was disabled in demo mode. It is now sessions + scrypt + CSRF tokens + roles + per-agency scoping on every query.
13. **Evidence tables were mutable.** Database triggers now make approvals, versions, envelopes, calls, observations, receipts, case events and trial results append-only.
14. **Reports were JSON only** and didn't show the "no usable transcript" denominator. There is now an HTML report and a PDF, broken down by manifest version.
15. **Several smaller gaps:** `handoff` was never accepted by the manifest API, there was no business-hours model, redaction missed one-time codes in common phrasings, and nothing marked customers as contacted.

## New
- The IWCB binding algorithm (see the README).
- A sandbox agent and sandbox CRM, so the full product can be demoed without live accounts and with everything labelled.
- A new UI covering the needs-action inbox, findings, finding detail, clients, the manifest editor with version chain, trials, the client report, integrity, the sandbox calendar and settings.
- 42 offline tests.

## Added: Problem A (semantic) and Problem C (execution)
- Business facts in the manifest. They are hashed, signed and approved like every other field, and shown on the client approval page.
- `facts.py` catches wrong prices, refund windows, cancellation notice and fees, insurance and hours, plus ungrounded answers given after a failed lookup.
- `ledger.py` is an append-only execution ledger. It holds one intent per commitment and counts the matching side effects across linked contact records.
- The ledger now detects double bookings from retries, bookings under a duplicate contact, a tool reporting failure when the write actually landed, and duplicate or missing confirmation texts.
- Fixed a contradiction: "false success" plus a verified booking now becomes a single retry-risk finding.
- Trials gained fact scenarios, and booking scenarios now fail on duplicate effects and missing texts.
- Findings are tagged A, B or C in the inbox, the queue and the detail page, and the queue can be filtered by problem.

## Added: the P0/P1 failure list
- Interruption- and timezone-aware booking extraction ("make it Thursday instead", "3 PM Eastern").
- Transfer outcome is read from the transfer tool's result, so a failed transfer counts as missed.
- Callback promises are checked against CRM tasks, with an SLA in the manifest.
- Manifest `booking_rules` (business hours, minimum notice, maximum days ahead, excluded services) and `escalations` are signed and approved like every other field. Violations are findings.
- Multi-intent calls: email, phone and callback requests are detected. Each one must end in a verified outcome, or Lane raises `incomplete_request`.
- `config_drift` compares the live prompt and tools with the approved manifest.
- Verify-to-close: confirming opens a case and never closes it. "Verify fix" runs a fresh read, an agent re-read, or a targeted retest trial.
- New trial scenarios for each of the above. The sandbox artifact ships the same features.
- 66 offline tests.

## Added: Vapi connector and public-data replay
- Vapi end-of-call-report webhook with Bearer, X-Vapi-Secret and HMAC auth, plus a read-only assistant snapshot. Agent APIs are platform-aware.
- A mock HighLevel API and an SGD replay harness that exercise the real webhook route and the real HighLevel client.
- Extraction fixes found by real-world phrasing:
  - spoken times of day ("quarter past 3 in the afternoon", "evening 6");
  - read-backs then "that is correct";
  - more completion phrasings, judged per sentence;
  - "No, that's all" no longer counts as an objection to the booking.
- 72 offline tests.

## Added: model as a second reader, with voting
- `LANE_EXTRACT_MODE=vote`: rules and model both read every call. The model's answer is checked against the transcript:
  - quotes are word for word, from the right speaker and in the right order;
  - the confirmation is not a question;
  - the date and time were actually said.
- The vote is confident when both readers agree, sends single-reader results to review, and abstains when the readers disagree.
- A model-found confirmation with a failed booking tool still raises false_success.
- `eval/compare_readers.py` gives a one-command before/after on the held-out set, with a cache.
- The default Anthropic model is now claude-haiku-5-5. 79 offline tests.

## Added: Groq
- `LANE_EXTRACTOR=groq` / `LANE_LABELER=groq` use Groq's OpenAI-compatible API (`GROQ_API_KEY`, default model llama-3.3-70b-versatile). `LANE_OPENAI_BASE` lets any OpenAI-compatible endpoint be used.
