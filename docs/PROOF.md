---
type: reference
updated: 2026-10-26
---

*Everything here was run on one Windows 11 PC on 2026-10-25 and 2026-10-26 (the spoken suite, its latency figures and the SIPp run were measured before the two review rounds' fixes; the unit suite, the guards and the probes after them): an RTX 5070 Ti shared with other work, Python 3.13.14, Pipecat 1.6.0, faster-whisper `small.en` on the GPU (float16), Kokoro through onnxruntime on the CPU, Ollama with `qwen3.5:4b`, and SIPp 3.6.1 from the Debian `sip-tester` package in Docker. Firms, callers, numbers and voices are invented. Each claim names the command that produced it.*

## 1. A real INVITE reaches the agent

SIPp is a separate program in a separate container, not our code. It places one call at the line and plays the recorded caller side of a reference call (speech as G.711, keypad digits as RFC 4733 events):

```
docker build -t sipclerk-sipp tools/sipp
python scripts/replay.py sipp_reference --json scratch/ref.json         # the reference call, with a softphone that waits for the agent
python tools/sipp/make_scenario.py
python scripts/make_sipp_media.py scratch/ref.json tools/sipp/media/call.pcap
python scripts/sipp_demo.py --out docs/sipp_run.json                    # SIPp exit 0 means every message it expected arrived
```

What the line saw ([`docs/sipp_run.json`](docs/sipp_run.json), full messages in [`docs/test-runs/sipp_messages.txt`](docs/test-runs/sipp_messages.txt)):

| Seconds | Direction | Message |
|---|---|---|
| 0.00 | in | `INVITE sip:+13055550102@192.168.65.254:5070`, SDP `m=audio 6000 RTP/AVP 8 0 101` (PCMA, PCMU, telephone-event) |
| 0.00 | out | `100 Trying`, `180 Ringing` |
| 0.00 | out | `200 OK`, SDP `m=audio 53799 RTP/AVP 0 101`: the line chose **PCMU**, its own first preference, not the caller's first offer |
| 0.00 | in | `ACK`, RTP starts |
| 15 to 75 | in | `OPTIONS` every 15 s from SIPp, answered `200` (keeps Docker Desktop's UDP mapping open, see below) |
| 87.32 | out | `BYE` from the line when the intake finished, answered `200` |

The call: 4,367 RTP packets in and 4,365 out (679,844 bytes of G.711 and events received), 11 keypad events (ten digits and the pound) made the phone number (`callback_number` came from the keypad, not from speech), five fields were spoken (name, practice area, state, urgent deadline, description) and email was skipped, the outcome was `intake_priority`, and the record carries tenant `tidewaterquint`, config version `2026.10.4` and config hash `75569928f389...`.

**Where the audio goes.** G.711 on the wire is 160 bytes per 20 ms packet, 64 kbit/s, SNR after a round trip about 37 dB for both codecs; decoded to 8 kHz linear; upsampled to 16 kHz in 20 ms frames for the voice activity detector and Whisper (relative error against a whole-signal resample 0.0000, 2 ms delay); Kokoro's 24 kHz output is resampled 3 to 1 to 8 kHz and encoded back. [`docs/codec_proof.txt`](docs/codec_proof.txt) is the output of `python scripts/codec_proof.py`.

**What this does not prove.** SIPp reached the line over a Docker bridge on one machine. A carrier's SIP trunk, a real phone number, NAT between a PBX and the line, jitter and packet loss from the public network were not tried. One finding is worth knowing: Docker Desktop forwards UDP through a NAT that forgets an idle mapping, so a BYE sent by the line 87 seconds after the last message from SIPp never arrived in the first runs. SIPp now sends an in-dialog OPTIONS every 15 seconds, which is what a phone or a session border controller does with a keepalive.

## 2. Tests

```
python -m pytest                    # 1718 passed, 25 deselected in 346 s
python -m pytest -m voice           # 25 passed in 1472 s (24 minutes 32 seconds), before the review rounds in sections 7 and 8; not replayed since
python scripts/mutation_check.py --layer all --report docs/mutation_report.json   # 220 of 220 killed, run in a copy of the repository
```

| File | Tests | What it pins |
|---|---|---|
| `test_guard.py` | 52 | one test per rule, several phrasings each, and every scripted line of both firms passing the output guard |
| `test_emergency_tense.py` | 77 | 30 narrative and 19 live emergency phrasings, every condition said with a past and a present subject, and the rule firing on fewer inputs than before (6,000 seeded sentences) |
| `test_machine.py` | 41 | read-back, retries, limits, keypad, silence, partial phone numbers, transfer results, qualification boundaries |
| `test_machine_property.py` | 163 | 160 seeded random calls over both firms, in and out of hours, with the invariants listed in the file, plus every record they produce being deliverable |
| `test_parsers.py` | 51 | names, numbers, dates, yes and no, practice areas, masking |
| `test_names_unicode.py` | 160 | 30 real names from several languages, with lead-ins and in decomposed form, and 4,000 seeded ASCII inputs read exactly as the old parser read them |
| `test_tenants.py` | 22 | schema, refusals, the hash following content not formatting, two firms sharing nothing, hours |
| `test_store.py` | 23 | encryption at rest, roles, a denied read leaving an audit line, a tampered audit chain, retention, and one unreadable call folder never stopping the purge |
| `test_retention.py`, `test_wiring.py` | 11 | the server applying retention at start and every interval on a fake clock, a failing pass retried, and every public method of the store, outbox and server being run by the line or listed with a reason |
| `test_delivery.py` | 36 | signatures against the two receivers' documented formats, backoff, dead letters, 25 seeded random failure worlds |
| `test_delivery_window.py` | 19 | a receiver down for ten minutes still gets the delivery, dead only after the 23 hour schedule, requeue by function and script, the dead count in the status |
| `test_outbox_at_rest.py`, `test_logs_carry_no_caller_words.py` | 10 | the raw outbox bytes holding no name, number, email or story in any state, bodies bound to firm and call, purge at retention, no log call carrying caller words |
| `test_dialled_number.py` | 88 | every written form of the dialled number for both firms, look-alike firms never confused, a `tel:` INVITE answered, an unparseable INVITE getting a 400, four shapes through the whole line |
| `test_route_set.py` | 11 | Record-Route echoed in the 200 OK in order, the BYE and ACK following the route set, strict and loose routers, the first route as the next hop |
| `test_anonymous_callers.py` | 11 | a withheld caller id never counting as a duplicate, a real number twice still refused |
| `test_transfer_leg.py` | 9 | the caller hanging up at every stage of a transfer: a CANCEL or BYE, no RTP afterwards, sockets and tasks back to the count before the call |
| `test_sip_stack.py`, `test_audio.py` | 20 | SIP parsing, SDP negotiation, a call over UDP with audio and digits, retransmissions, G.711, resampling |
| `test_llm.py`, `test_processors.py` | 32 | the language model's fences, what the Intake processor refuses to say, what the Stt processor refuses to hear |
| `test_pipeline_fast.py` | 18 | the whole line with real SIP, RTP and voice activity detection and stand-in speech models, including an abandoned transfer and two anonymous callers |
| `test_bridge_contract.py` | 3 | our delivery against the real verification code of `clio-matter-bridge` (skipped without node and the sibling checkout) |
| `test_voice_suite.py` | 25 | the spoken scenarios below |

## 3. The spoken scenarios

Each scenario is text in `voice_scripts/scenarios.py`, rendered to WAV with Kokoro voices that are not the agent's, and replayed over real SIP and RTP in real time. Assertions read the saved call record, not the audio.

| Scenario | What it does | Call length |
|---|---|---|
| `normal_intake` | a full intake | 77.4 s |
| `normal_intake_second_firm` | a full intake | 89.8 s |
| `barge_in` | the caller talks over the phone number question | 71.0 s |
| `long_silence` | the caller is silent after the recording question, then carries on after the reprompt | 90.5 s |
| `abandoned_silence` | the caller never answers; the agent ends the call and leaves a callback task | 37.5 s |
| `noisy_line` | white noise at 15 dB signal to noise ratio on every caller clip | 75.9 s |
| `caller_correction` | a full intake | 81.2 s |
| `invalid_answers` | a first name only, six digits of a phone number, and a date in the future | 94.2 s |
| `unsupported_practice_area` | a full intake | 63.7 s |
| `legal_advice_request` | asks for legal advice at the first question; the agent declines and carries on | 83.7 s |
| `legal_advice_repeated` | a second request for advice hands the caller to a person | 50.1 s |
| `existing_client_transfer` | warm transfer to the firm's desk phone, answered | 37.3 s |
| `transfer_busy` | the desk phone is busy: a callback task and an honest line, no dead air | 28.6 s |
| `transfer_no_answer` | the desk phone rings out | 34.5 s |
| `dtmf_phone_number` | a full intake | 72.2 s |
| `dtmf_zero_for_a_person` | press zero at any moment for a person | 30.5 s |
| `provider_stt_timeout` | speech to text times out on the name: the agent asks again, the caller repeats it, and the call completes | 83.1 s |
| `provider_llm_error` | the language model is down: plain parsing carries the call, the staff summary falls back to a template | 77.4 s |
| `provider_webhook_500` | the case-management receiver fails three times: the call is unaffected and the delivery is retried | 76.4 s |
| `after_hours` | the office is closed: the agent says so and takes the intake for a callback | 82.3 s |

### Latency, measured at the caller

| | Turns | Median | 95th percentile |
|---|---|---|---|
| All turns, as the caller hears them | 141 | 619 ms | 1611 ms |
| Agent side: detector end of speech to first audio, no model in the turn | 125 | 151 ms | 267 ms |
| Agent side: the language model in the turn | 14 | 572 ms | 1019 ms |

Measured from the last RTP packet of the caller's sentence to the first loud RTP frame of the agent's reply. The silence that ends a turn is part of the figure (0.5 s for a yes or a name, 1.0 s for a number, 1.2 s for a story), so the median is mostly waiting, not compute. This machine was running other work, and the line and the test caller share one event loop; treat the numbers as this machine's and no one else's.

### What one call costs, measured

Mean over 13 completed intake calls:

| | Per call |
|---|---|
| Time on the line | 80.4 s |
| Language model calls | 1.08 |
| Prompt and completion tokens | 82.62 and 14.23 |
| Language model time | 0.28 s |
| Transcriptions and their compute time | 9.46 and 1.47 s |
| Synthesised agent speech | 45.54 s |
| Caller audio received | 80.71 s |

Tokens and seconds, not dollars: nothing here is billed.

## 4. The mutation check

220 deliberate breaks, one at a time (182 in the unit layer, 38 through the real line with stand-in models), run in a temporary copy of the repository so the working tree is never edited. A mutant counts as killed only when pytest ran and a test failed; a crash or a collection error does not count. The report with the test that killed each one is [`docs/mutation_report.json`](docs/mutation_report.json). The report merges one full run of all the unit rows with a second run, on the final code, of every pipeline row and the unit rows on `call.py` (the first run was made before a three line change to `call.py` was dropped again). Rows in `scripts/mutants_derived.py` (108 of them) take their edit from the code's syntax tree with `scripts/mutate_ast.py` (a function, a kind of change and a snippet of the code at that site), so rewording a line moves the site instead of leaving a typed old text that matches nowhere.

Every guard rule, every branch of the state machine that decides something, every field parser check, every tenant, storage and delivery rule, and the barge-in, silence, keypad, recording, transfer and routing paths in the pipeline each have a mutant. The check found seven test gaps while it was being built, each now closed: an unclear recording answer (the test sent "I don't know", which the parser reads as a no), an unsupported practice area never driven through the machine, the callback task of a failed transfer never asserted, duplicate RTP packets, a recording gate that a second gate hid (found by timing how much of the greeting was in the recording), a failed transcription that the test did not require an answer to, and the refusal of a call with no common codec. One mutant was written and dropped as equivalent (`scripts/mutants_found.py` says why), and a branch that could not be reached was deleted instead of tested.

Not covered: a mutant whose failing test leaves a call open can hang the test process instead of failing (the run is then reported as an error after its timeout, and was rerun on its own: it was killed each time); the mutants break code, not configuration, and the voice scenarios are not part of the mutation loop (a mutant that needs a 25 minute run is not a check anyone reruns).

## 5. The two receivers

[`docs/integration_run.txt`](docs/integration_run.txt) is one call delivered over real HTTP: `clio-matter-bridge`'s own verification code (imported, not copied) accepted the signed delivery and called a redelivery a duplicate (`tests/test_bridge_contract.py`), and the `clinic-call-console` API, built from its repository and run from a scratch copy, created the call (`201`), called the repeat a duplicate (`200`), refused a forged, a stale and a missing signature (`401`), and showed the transcript masked ("My name is [NAME]", "[number]").

No Clio record was written. The stored Clio developer trial token was tried once with a read (`who_am_i`) and answered `401`; the trial ended in August 2026.

## 6. What was not proven

- A real phone number, a carrier trunk, or any traffic that was not generated on this machine.
- A real caller: every voice is synthetic, English, and the same two Kokoro voices speak for the agent.
- Accents, interruptions by a second person, background speech, music on hold, a mobile network.
- Anything about LiveKit, Asterisk, FreeSWITCH, Twilio, Telnyx, Deepgram or a hosted model: none was run.
- Behaviour under load: two simultaneous calls were run, not twenty.
- The guard against anything it has no rule for.

## 7. The review round

A reviewer who read the files instead of the code, and ran probes against a copy of the repository, found eleven things that mattered on an ordinary day; the first round fixed all eleven, each with a test that fails on the old code and a mutation row that is killed. What was measured, from the probes of that review replayed against this repository:

| Finding | Before | After |
|---|---|---|
| Retention never ran | `TenantStore.purge` had no caller | applied at start and every hour; a call older than its firm's `retention_days` is gone, the other firm's window holds (`test_retention.py`) |
| One half written call folder | `purge` raised `FileNotFoundError`, no firm lost any call | the folder is skipped and counted, both firms purged |
| Caller data in the outbox | name, phone, email and story in plaintext, kept after delivery | the raw file holds none of them in any state; delivered entries keep id, status and times |
| Delivery given up | dead after 6 attempts at 31 seconds | delivered at attempt 11 when the receiver returned after 10 minutes; dead only after 83,295 s; `requeue` and `scripts/requeue_dead.py` |
| Transfer leg after hang-up | desk INVITE never cancelled, the desk phone answered a dead call, 101 RTP packets in 2 s kept arriving | CANCEL, then no answer, no RTP, the callback task still created |
| Names | `Daniel Muñoz` stored as `Daniel Mu Oz`, `José García` refused twice | both read back and stored as said |
| Dialled number | `s@host` with the number in To, ten digits and `tel:` all failed, `tel:` with no answer at all | each answered and routed to the right firm |
| Route set | no Record-Route in the 200, BYE without Route | echoed in order, BYE carries them |
| Anonymous callers | the second got 486 Busy Here | both answered |
| Emergency rule | `taken to the emergency room` got the 911 line and a transfer | the intake completes; `he is bleeding` and 18 other live phrasings still escalate |

Left alone on purpose, with the reason: a torn last line of the audit file (needs a crash during an append), a new SSRC with a random sequence number, a redial 0.3 s after a dropped call, a 30 ms packet time, date guesses without a read back, and the group of DTMF, SDP and memory items, each scored low by the review; and every limit in the README.

The spoken suite (section 3) was not replayed after this round. The phrasings it speaks are all in the text replay used for the impact check: the machine and guard fed the 21 scenario scripts and 25 everyday utterances give the same answers before and after.

## 8. The second review round

A second reviewer, who had not seen the first one's findings, ran probes against a copy of the repository after the first round's fixes and found eight more things that mattered on an ordinary day (the rest of its list was scored low and left alone, below). Each fix has a test that fails on the old code, a table or property test over the class, and mutation rows that are killed. What was measured, from the reviewer's own probe scripts replayed against this repository:

| Finding | Before | After |
|---|---|---|
| `my back is hurt`, `my kids are hurt too` | the 911 line and a transfer (after hours the intake was cut short) | the intake goes on; `he is badly hurt`, `my son is hurt, please help`, `he is not breathing` still escalate (`test_emergency_report.py`) |
| `an emergency hearing`, `an emergency protective order`, `kind of an emergency` | the 911 line and a transfer | the intake goes on; `this is an emergency` still escalates |
| An ACK at 6 seconds, or never | no session, no media, no BYE, no record; 11 resent 200 OKs and a silent drop | the call starts on the late ACK; with none, BYE after 64*T1, media freed, a `dropped_no_ack` record with a callback task (`test_ack_window.py`, T1 shortened to 0.05 s and 0.2 s) |
| A caller who hangs up mid intake, or during a transfer | a stored callback task and a console delivery only | the bridge gets it too, as `intake.incomplete` with the task (`test_outcome_receivers.py`, every outcome the machine can assign) |
| A blocked number sent as ten digits | answered with 200 OK while the `+1` form got 603 | 603 (`test_caller_number.py`) |
| `+14155550142;cpc=ordinary` as the caller id | stored as an empty number, skipped the block list and the one call per number rule | stored as `+14155550142`, counted as one caller |
| `An Nguyen`, `Thi An Tran`, `Kevin Case`, `Mary Hurt`, `Daniel To` | refused twice, then a transfer | read back and confirmed (`test_names_stopwords.py`, model fallback included) |
| A personal injury caller whose description said the other driver was arrested for DUI | declined as not an area the firm handles | the intake completes (`test_scope_confirmed_area.py`) |

The RFC text behind the ACK fix is 3261 section 13.3.1.4: the 2xx is resent from T1 up to T2 until the ACK arrives, and "if the server retransmits the 2xx response for 64*T1 seconds without receiving an ACK, the dialog is confirmed, but the session SHOULD be terminated", with a BYE. Two things the longer ACK wait made easy to hit were fixed with it and are pinned in `test_ack_window.py`: the one call per number rule now counts an answered, unconfirmed caller as on the line (a redial behind the first call's ACK was answered), and a line that is stopped no longer leaves a voice pipeline running (`stop` waits for its live calls to file their records, and closes one that comes out of its ACK wait) (a mutation guard on the blocked caller check found it, because the test process hung at the end of the event loop instead of failing).

Impact check, over inputs an ordinary day brings: every text row of both kept corpora, the 21 scenario scripts through the machine, 25 everyday utterances and 25 more written for the check (through the guard of both firms and the name reader), and 25 whole calls. Run against a copy of the code before the fixes and one after: 203 inputs, 179 identical, 24 different, none of the 24 a new refusal; the 24 are the findings above (a refusal the code no longer makes, or a name it now reads) and two whole calls whose description named a visa or a divorce after a confirmed car accident, which are now taken instead of declined. The corpus rows the old code already handled as expected (6 with a stated expectation) did not change. The emergency rule's seeded comparison (6,000 random utterances and every pinned phrasing) shows it fires on strictly fewer inputs than before and on none it did not fire on before, and every pinned live phrasing (19 plus the two the reviewer tested) still fires.

Left alone on purpose: a mid call re-INVITE to a new media port is answered with the old SDP, `October 30th` said on October 26th reads as the future, a CANCEL right behind the INVITE, a burst of INVITEs past `max_calls`, the SIP stack's maps that are never pruned, a negated `call 911` ("I did not call 911") that still fires, and `fifteen days ago` or `eighteen months ago` in the plain date parser (the model fallback reads them). Sibling phrasings of the emergency class found by sweeping the rule and not fixed are listed in DECISIONS.md, with `someone is attacking me`, which the rule misses. The spoken suite was not replayed after this round; the impact check above is the text replay of what it speaks.
