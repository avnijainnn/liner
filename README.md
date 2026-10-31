# Vaada

Vaada holds AI phone agents to what each business approved, and checks that what they promised actually happened in the business's records.

Each client gets a **manifest**: a versioned, hashed, Ed25519-signed list of what its agent may do, ask and promise, approved by the business (or by the agency on its behalf, and always shown as such). Vaada uses it twice:

- **Trials** compile a test call from every manifest rule, run each 2–3 times, and judge the transcript and a fresh calendar read.
- **Monitor** binds every real Retell call to the manifest version and agent version in force when the call *started*, runs the transcript checks, and confirms that bookings landed in HighLevel. Vaada is read-only: it never writes to a customer system.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env            # set VAADA_SECRET_KEY
uvicorn app.web:app --reload    # http://localhost:8000
```

Click **Open sandbox workspace** on the sign-in page. You get a sandbox agency with three synthetic clients, a week of simulated calls, two trials and open cases. Everything sandbox is labelled.

Production layout (Postgres plus a separate worker):

```bash
docker compose up --build       # web :8000, worker, postgres
```

Tests: `python -m unittest discover -s tests -v` (79 tests, all offline).

## The 3-minute demo, screen by screen

1. **Manifest** (Clients → Bright Smile Dental → Manifest). v2 was approved by Dr. Priya Patel (client) for agent v14. The version chain on the right shows the hash, prev-hash, signature and effective interval of each version. Click **Edit a copy**, tick a box, then **Create approval link**. Open the link in a private window to see what the business owner sees, and approve.
2. **Trial**: click **Run trial**. 17 scenarios × 3 runs. Failures show the rule, the transcript clip and promised-vs-calendar. Flaky scenarios are marked. Use **Share with client** to make a read-only link.
3. **Monitor**: on **Needs action**, the top card is "not 9, make it 10". The caller agreed 10:00 and the calendar shows 9:00, so the finding is contradicted, stamped with manifest v2 and agent v14.
4. **Fix and verify**: click **Fix in sandbox HighLevel**, move it to 10:00, click **Re-check calendar** and the case closes as verified. Customer contact is tracked separately.
5. **Client report**: shows denominators, a breakdown by manifest version and who approved each one. **Export PDF**.
6. **Integrity** (manifest page): re-derives every binding.
7. **Lone Star Plumbing**: the running agent v8 gained a `cancel_appointment` tool that the manifest never approved. That raises one *capability-expansion* drift finding, not one per call.

## Three problems, three checks

| Problem | Question | How Vaada checks it | Findings |
|---|---|---|---|
| **A · Semantic** | Was what the agent *said* true for this business? | Client-approved **business facts** in the manifest (prices, refund window, cancellation notice and fee, insurance, hours). `facts.py` extracts factual claims from agent sentences and compares them deterministically; no approved fact means no verdict. A claim made right after the agent's own lookup failed, and not backed by a fact, is flagged as ungrounded. | `wrong_information`, `ungrounded_answer`, plus forbidden asks and promises |
| **B · Outcome** | Did the promised thing actually happen? | Fresh reads of the record: the calendar for bookings, reschedules and cancels; the conversation for promised text confirmations. | `booking`, `message_promised`, `false_success`, `unrequested_booking`, `missed_handoff` |
| **C · Execution** | Did it happen exactly once? | The **execution ledger** (`ledger.py`) records an intent per commitment and, on every read, counts the real side effects across contact records that share the caller's phone. Verdicts: none, once, duplicate, landed despite failure. Retries in the tool trace are counted. Every reconciliation is appended to `ledger_entries`. | `duplicate_effect`, `retry_risk` |

Vaada is read-only, so C is detection, not prevention. Stopping a duplicate before it happens means putting Vaada in the call path, which is the v2 pre-action check.

## Coverage: the ten failures Vaada is built to catch

Vaada's promise: catch the important things voice agents get wrong, miss, or claim to have completed, and give agencies evidence and a workflow to fix them.

| # | Failure | How Vaada catches it | Finding type | Closes when |
|---|---|---|---|---|
| P0·1 | Wrong booking (Tuesday → Thursday, wrong timezone) | Dialogue-state tracker (corrections, interruptions, caller timezone) vs a fresh calendar read | `booking` | A fresh read matches what the caller finally agreed |
| P0·2 | False booking confirmation | Agent said "booked" but the tool failed and no record exists | `false_success`, `booking` | Calendar read shows the booking |
| P0·3 | Missed human transfer | Caller asked for a person (or a transfer failed) and no successful transfer happened | `missed_handoff` | Retest trial: transfer scenarios pass 3/3 |
| P0·4 | Unfulfilled callback promise | "Someone will call you back" vs CRM tasks: missing, open past the SLA, or completed | `callback_promised` | Task read shows it completed |
| P0·5 | Business-rule violation | Approved hours, notice, booking window, excluded services, escalation phrases | `rule_violation`, `missed_escalation` | Retest trial for that rule passes 3/3 |
| P0·6 | Incomplete request | Every caller request (appointment, email, phone, callback) needs an outcome | `incomplete_request`, `contact_update` | CRM read shows the update / retest passes |
| P1·7 | Duplicate or uncertain action | Execution ledger counts side effects across linked contacts; retries and timeouts | `duplicate_effect`, `retry_risk` | A fresh count shows exactly one effect |
| P1·8 | Missing downstream update | Promised SMS and CRM updates are read back | `message_promised`, `contact_update` | Read shows the message / field |
| P1·9 | Outdated information | Agent claims vs approved, hashed business facts; answers after failed lookups | `wrong_information`, `ungrounded_answer` | Retest of the fact scenario passes |
| P1·10 | Configuration drift | Live agent version and live prompt vs the approved manifest (stale facts, removed services, missing transfer tool) | `agent_drift`, `config_drift` | A fresh read of the live agent matches |

**Trials** cover corrections, interruptions, unavailable slots, transfer failures, policy restrictions, multi-intent calls, timezone edge cases, callbacks, escalations and every approved fact. Each scenario runs several times, flaky results are labelled, and a confirmed finding reruns only the scenarios for the rule it broke.

**Monitor** compares four things for every booking: what the caller requested, what the agent promised, which tools ran (and whether they succeeded), and what the record actually shows. Only the record can verify.

**Needs-action inbox** shows the client, evidence, severity, owner and case status, with problem counts and patterns across clients. Confirming a finding opens a case. It never closes it. A case closes only when an independent check passes: a fresh record read, a re-read of the live agent, or a retest trial.

**Out of scope:** speech latency, voices, accents, background noise, speech-to-text accuracy and telephony quality. Vaada judges what was said and what happened, not how it sounded.

## Connectors: Vapi + HighLevel, tested on public dialogue data

**Vapi** (`app/integrations/vapi.py`). Set the assistant's Server URL to `/webhooks/vapi/<agency key>` and enable `end-of-call-report`. Vaada accepts Vapi's three credential styles:
- Bearer token;
- legacy `X-Vapi-Secret`;
- HMAC-SHA256 in `x-signature`, with an optional timestamp.

Unauthenticated posts are rejected. Transcript turns, tool calls and their results map into the same evidence Retell calls produce, so every check runs unchanged. The Vapi API key is only used read-only, for the assistant prompt and tools, for drift checks and auto-drafts. Field names follow Vapi's official server SDK.

**HighLevel** (`app/integrations/highlevel.py`) is read-only by construction. `eval/mock_highlevel.py` serves the same endpoints, auth and envelopes locally, so the real client can be tested over HTTP.

**Replay test** (`eval/sgd_replay.py`). This replays human-written appointment dialogues from Google's Schema-Guided Dialogue dataset (hair salons and therapists, CC BY-SA 4.0). Each one goes as a signed Vapi webhook into a running Vaada server, which reads the mock HighLevel API. The SGD booking calls give the exact agreed date and time. One fault is injected per call: wrong time, wrong date, dropped write, false success, wrong contact or cancelled. Calls are kept only when they contain exactly one appointment booking.

| Set | Calls | Verdict accuracy | Faults caught | False alarms | Date+time read exactly |
|---|---|---|---|---|---|
| SGD train split, held out, blind | 117 | 71.8% | 57/81 | 4/36 | 64.5% |
| Same, after one fix found by it | 117 | 74.4% | 58/81 | 2/36 | 69.2% |
| SGD dev+test, used for tuning | 96 | 89.6% | 50/58 | 0/38 | 78.7% |

Quote the blind held-out row. Most misses are calls where Vaada recognises no confirmation, so it stays silent rather than raising a false accusation. That is where the optional LLM fallback (`VAADA_EXTRACTOR`) should be measured next. Run it with `python -m eval.sgd_replay <path to dstc8-schema-guided-dialogue/train>`.

**Held-out test 2: Taskmaster-1 auto-repair** (`eval/taskmaster_eval.py`, CC BY 4.0). These are 699 spoken two-person dialogues; the caller side has real speech-recognition errors. Gold is the annotated *accepted* date and time. It measures reading only, and it was run blind, with no tuning on this set.

| Of 568 machine-readable bookings | Rules only |
|---|---|
| Exact date and time | 41.9% |
| When Vaada states a time, it is right | 72.8% (238/327) |
| No confirmation found / abstained | 147 / 94 |

The main error: the agent offers or confirms a different slot from the one the caller first asked for ("fully booked today… tomorrow at 3pm"), and the rules keep the caller's original request. A smaller share comes from date conventions such as "next Tuesday". This is the next target for the model reader. Run `python -m eval.taskmaster_eval <Taskmaster>/TM-1-2019/woz-dialogs.json`, adding `VAADA_EXTRACTOR=anthropic` to compare.

## Two readers: rules + model (optional)

Set `VAADA_EXTRACTOR=groq` (and `GROQ_API_KEY`; default model `llama-3.3-70b-versatile`, change with `VAADA_GROQ_MODEL`), `anthropic` (`ANTHROPIC_API_KEY`) or `openai` (`OPENAI_API_KEY`) to add a model as a second reader of every call. `VAADA_EXTRACT_MODE=vote` is the default. The model never decides a verdict. It only reads the call, and its answer is thrown away unless all of these hold:
- its two quotes are word for word in the turns it cites, from the right speaker;
- the agent's confirmation comes after the caller's final words, and is not a question;
- the date and the time it reports were each actually said in the call.

Then the two readers vote:

| Rules | Model | Vaada does |
|---|---|---|
| same date and time | same date and time | confident result |
| found a booking | found none | uses rules, marks `single_reader`: review before telling the client |
| found none | found a booking | uses model, marks `single_reader` |
| different times | different times | abstains: the booking shows as "unknown, check by hand", never "contradicted" |
| model unreachable or failed a check | | rules only (`model_unavailable`) |

The record check (contact, calendar, time, status) is always code. A model mistake can make Vaada say "not sure". It cannot make Vaada accuse anyone.

To measure it, run `python -m eval.compare_readers <path to SGD train>`. This runs the held-out replay twice, rules only and then rules plus model, and prints a side-by-side table with token counts. Model answers are cached in `eval/.llm_cache.json`, so re-runs are free and repeatable. The current rules-only baseline on the held-out set is in the table above.

## Labelled evaluation set (synthetic)

`eval/data` holds 6 labelled synthetic datasets (128 cases, 350 checks): extraction challenges, booking verification cases, a 72-call accountability regression, a 3-client agency world and canned trial responses. Run `python -m eval.run_eval`.

The first blind run scored 342/350 (97.7%). The 8 misses were in extraction: confirmations like "I booked…", "booked for…" and "Appointment confirmed.", and callers who answered with a bare "November 4 at 3 PM.". After fixing those, the score is 350/350. Because the fixes were made against these same cases, quote the blind score, not the 100%. None of this is real call data.

## IWCB: the manifest-binding algorithm (`app/binding.py`)

Interval-Witnessed Capability Binding decides which approved version judges a call, and makes that decision tamper-evident and re-derivable.

1. **Interval resolution, no fallback.** Approved versions form half-open intervals `[from_i, from_{i+1})`. Binary search on the call's *start* time. If no version was in force, coverage is Unknown. Vaada never uses "the latest" version.
2. **Boundary dual-judgment.** Retell's clock stamps call starts and Vaada's clock stamps approvals. A call that starts within ±ε (default 90 s) of a version boundary is judged under *both* adjacent versions. A finding stands outright only if both agree. Otherwise it is held for review with a boundary note, so clock skew never silently decides which rules applied.
3. **Capability-surface drift.** Vaada fingerprints the agent's behavioural surface (normalised prompt and tool contract) and maps tools to capabilities. Drift is classified as `renumbered`, `prompt_changed`, `capability_contraction`, `capability_expansion` or `version_mismatch`, and severity follows the class. A re-published identical agent is low severity. A newly added cancel tool is high.
4. **Witnessed receipt chain.** Each binding writes `r_n = H(r_{n-1} ‖ canonical(payload))`. The payload holds the chosen hash, its neighbours, the chain head at bind time and the drift class. Every receipt can be re-derived from the version table, so the integrity check catches the attack that plain hash chains miss. That attack is a correctly chained, correctly signed new version with a **back-dated** effective time. Daily Merkle roots let a report commit to all of a day's bindings in one hash, and they are what a v3 public log would publish.

Approval also refuses to take effect before the newest binding receipt, so a back-dated version can't be created through the product at all.

## What's in v1 (spec Part 1) and beyond

| Spec item | Where |
|---|---|
| Manifest fields, canonical SHA-256, versions, prev-hash chain | `manifest.py`, `crypto.py` |
| Ed25519 envelope per agency key (outside the hashed body) | `crypto.py`, `manifest.approve` |
| Client approval link with exact-hash confirmation; agency-on-behalf, always labelled | `/approve/{token}`, manifest screen |
| Auto-draft from Retell prompt/tools + HighLevel settings | `manifest.auto_draft`, **Sync** |
| Call-start binding, agent-version drift, findings stamped | `binding.py`, `pipeline.py` |
| Forbidden ask, unauthorized promise, empty promise, missed handoff (flag only, deterministic rules over labels) | `labeler.py`, `checks.py` |
| Booking landed: final accepted request (corrections, fallbacks, questions≠requests, DST-safe) | `extract.py` |
| Fresh reads at 2/15/60 min, compressed for near appointments; states pending/verified/contradicted/unknown/flagged/superseded/waived | `verify.py`, `worker.py` |
| Fix-and-verify cases, owner, due time, customer-contact tracking, dispute keeps evidence | `cases.py` |
| Trials compiled from the manifest, 2–3 runs, flaky detection, diff vs last trial, shareable | `trials.py` |
| Client report with denominators + PDF | `reports.py` |
| Six screens + needs-action inbox | `templates/` |
| Security: sessions, scrypt, CSRF, tenant scoping, signed webhooks, secrets encrypted, redaction, append-only evidence triggers | `web.py`, `db.py`, `pipeline.redact` |
| v1.5: reschedule/cancel, duplicates, false success, unrequested booking, supersede | `extract.py`, `verify.py`, `pipeline._supersede` |
| v2: manifest → prompt guardrails | manifest screen |

## Going live with one agency

1. Settings: paste the agency's Retell API key and point Retell's webhook (`call_ended`) at the URL shown.
2. Add the client with its Retell agent ID, HighLevel location ID, calendar IDs and a **read-only** private-integration token.
3. Press **Sync agent + calendars**, then **Auto-draft**, then send the approval link.
4. For live Trials, add a `trial_line` object to the client's agent snapshot with `agent_number`, `caller_agent_id` (a Vaada-owned Retell agent whose prompt reads `{{vaada_script}}`) and `from_number`.

## Honest limits

- The Retell signature check mirrors `retell-sdk`'s `verify` (`v=<ms>,d=<hmac>`), and Vaada uses the SDK when it is installed. Still confirm it against a real webhook from your account.
- HighLevel endpoints and `Version` headers follow the public docs. Verify them against one real sub-account before a pilot.
- The Postgres path is written for, but only SQLite was exercised in this build.
- HighLevel contact search (`GET /contacts/?query=`) and conversations endpoints are used read-only for identity linking and message checks; confirm the scopes (`contacts.readonly`, `conversations.readonly`, `conversations/message.readonly`) on a real sub-account.
- The fact checker understands a fixed set of fact types and phrasings. Anything else is "unverified", never "wrong".
- The rules labeller and extractor are deterministic baselines. Measure precision on real labelled calls before claiming a number. The LLM backends are optional, and their output is accepted only when its quotes appear verbatim in the transcript.
- Signing keys are encrypted with `VAADA_SECRET_KEY`. Move them to a managed secrets store before production.
