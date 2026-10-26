# Layout

What is where, file by file at folder level.

```
sipclerk/sip/        SIP messages and SDP, the user agent, RTP with G.711 and RFC 4733, the softphone
sipclerk/intake/     the state machine and the field parsers
sipclerk/voice/      Pipecat processors, shared Whisper and Kokoro, the language model fences
sipclerk/guard.py    the guard, before and after the model
sipclerk/tenants.py  tenant files, schema, hash, lookup by dialled number
sipclerk/store.py    encrypted per-tenant storage, roles, audit chain, retention
sipclerk/delivery.py signed deliveries with an outbox
sipclerk/server.py   the listener, retention and outbox loops
sipclerk/call.py     one call from INVITE to hang-up, the transfer leg
sipclerk/harness.py, labrun.py  the test caller and the lab runner used by replay.py and the tests
tenants/             the two invented firms and the JSON schema
tests/               the suite, its class tables and the replayed corpus in tests/corpus
demo/, brand/        the demo recording build, and the brand file (no icon)
.github/             CI and Dependabot
requirements.txt, requirements-voice.txt   what CI installs, and what the spoken suite adds
voice_scripts/       the spoken scenarios and the rendered WAV clips
scripts/             replay, mutation check, SIPp media, integration checks, requeue of dead deliveries
tools/               SIPp in Docker, the bridge receiver
docs/                PROOF.md, DECISIONS.md, the run logs
```
