---
type: reference
updated: 2026-10-25
---

*What was chosen, why, and what each choice costs. Every limit here is a limit of this lab.*

## The SIP stack is our own, small, and UDP only

**Chosen:** a minimal SIP user agent in `sipclerk/sip/` (parser, SDP offer and answer, INVITE as server and client, CANCEL, BYE, OPTIONS, INFO, retransmission handling) and an RTP session with G.711 and RFC 4733 digits.

**Why not LiveKit, Asterisk or FreeSWITCH:** the goal was a real INVITE reaching the agent with nothing bought and nothing hosted. A self-hosted LiveKit server plus its SIP service needs Redis, a wide UDP port range and host networking, which Docker Desktop on Windows handles badly, and it would be a platform to configure rather than telephony to show. Asterisk or FreeSWITCH would put a PBX between the caller and the code, so the code would never see an INVITE. The user agent is under 1,000 lines (`message.py`, `rtp.py`, `stack.py`), small enough that every branch can be tested and mutated.

**What it costs:** no TCP, TLS, REGISTER, digest authentication, SRTP, forking, session timers or REFER, and only the transaction behaviour the lab needed. A production line would sit behind a carrier or session border controller (Twilio Elastic SIP Trunking, Telnyx, a LiveKit SIP service) that speaks plain SIP to this process, or this module would be replaced by that platform. None of those was run here.

**SIPp** (GPL-2.0, from the Debian package, in Docker) is the second, independent SIP caller, so the INVITE is not only ever produced by the code that answers it.

## Pipecat carries the media, the speech models are shared

The pipeline is `RtpInput, Vad, Stt, Intake, Tts, RtpOutput`, all Pipecat frame processors. Pipecat's own Whisper and Kokoro services load a model per instance, which is a cold start on every call, so the services here borrow one `faster-whisper` model and one `kokoro-onnx` model loaded at start. Voice activity detection is Silero, bundled with Pipecat, with `stop_secs` 0.5 so a pause inside a phone number does not end the turn.

Pipecat's streaming resampler was tried first for 8 kHz to 16 kHz and swallowed speech that followed a long stretch of digital silence (found with a 12 second lead of zeros before a clip). `audio.StreamUpsampler` resamples each 20 ms frame together with the tail of the earlier ones and costs 2 ms. A test pins that it matches a whole-signal resample and leaves no click at a frame edge.

Fixed prompts are rendered once, sentence by sentence, and cached: a long line is several cached sentences plus the one sentence that changes ("I have Jordan Reyes."). The numbers report cached and rendered turns separately.

## The model never drives the call

The state machine in `sipclerk/intake/machine.py` decides what happens next. The language model has four fenced jobs, each with a time limit, a validator and a fallback: classify a description into the tenant's practice areas when the keyword reader cannot, read a name or a date the plain parser could not, write one acknowledgement sentence before the closing line, and write the staff summary after the call. An acknowledgement the output guard blocks is dropped, not replaced. With the model down the call completes on plain parsing and the summary falls back to a template (a test and a voice scenario both pin that).

## The guard is keyword rules, on purpose

Before the model: emergencies, existing clients, a request for a person, a request for legal advice, and an unsupported practice area. After the model, before the voice: promised outcomes, legal advice, an implied attorney-client relationship, a commitment nobody made. Rules are named lists of patterns so the mutation check can delete one at a time and a test must notice. A blocked agent line is replaced by a fixed safe sentence.

**What it costs:** a keyword rule misses a rewording it was not written for, and it can fire on an innocent phrase (the word "operator" in a story about a truck operator asks for a person). A classifier would be better at both and cannot be audited line by line. The first version of the emergency rule fired on "I was hurt in a crash", which is most personal injury calls; the test that found it is `test_out_of_scope_is_only_checked_when_asked_and_in_scope_words_win`.

## A tenant is a file

`tenants/*.yaml`, validated by pydantic models that forbid unknown keys, with a JSON schema exported from the same models. The SHA-256 of the canonical form (sorted keys, no whitespace) and the version are stamped on every record, so a call can be replayed against the exact configuration that handled it. Two firms are loaded at start and two firms with one number is a start-up error. A firm's questions, prompts, transfer destination, hours, recording rule, retention and roles never reach the other firm's call (a test asserts it with two simultaneous calls).

## Warm transfer is a bridged second call

The agent places an INVITE to the tenant's desk phone, speaks a short whisper to staff only, then relays RTP between the two legs, transcoding between PCMU and PCMA when the legs differ. The caller hears a soft ringback while it rings. A busy or unanswered desk phone leaves a callback task and an honest line, never dead air. REFER (handing the caller to the desk phone and leaving) is not implemented.

## Storage, access and the audit line

Every part of a saved call (record, transcript, summary, recording) is AES-256-GCM encrypted under a key derived per tenant, with the tenant, call and part bound in as associated data. Reading goes through `TenantStore.read(actor, ...)`: the actor must belong to the tenant and hold a role the tenant grants for that part, and every read, allowed or denied, writes one hash-chained audit line. A retention setting per tenant deletes expired calls and audits the deletion. The master key is a local file in `.secrets/`; a real deployment would use a key management service.

## Delivery never touches the call

A finished intake is queued in an outbox with an idempotency key (tenant, call, intake), sent to two receivers, and retried with backoff on a timeout or a 5xx. A 4xx that will not improve (a bad signature) becomes a dead letter at once. The outbox is a file, so a restart does not lose a pending delivery. A property test over seeded random failure sequences found a real bug: an empty outbox was written as one blank line and could not be read back after a restart.

The two receivers are used the way their repositories document them: `clio-matter-bridge` verifies `x-hook-signature` (HMAC-SHA256 of the raw body) and dedupes on `meta.event`, `data.id` and `data.etag`; `clinic-call-console` takes `POST /api/ingest/calls` with `X-Signature: t=<unix>,v1=<HMAC>`. No Clio account write happens: the stored Clio developer trial token answers 401 (the trial expired in August), so the intake goes to the bridge's own verification code as a Clio-shaped payload.
