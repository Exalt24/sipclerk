# Limits found by the last readers

Each item below was reproduced against this repository by running the code or reading the lines named, on 2026-10-26. They are known limits of a lab on invented firms and callers, not fixed here. The ones a reader found and the lab did fix (a spoken email with words after it, a numeric date, a date said in two units) are tested in `tests/test_email_and_date_shapes.py`.

## What the caller says

- **Dates.** A date answer has no read-back. A year alone is stored as 30 June of that year. A date said in two units (`two weeks and three days ago`, `three years and two months ago`) and a numeric date (`12/15/2023`, month first) are read whole; a date said any other way is taken as heard.
- **Email.** The address is never read back, and no email is ever sent: the Tidewater prompt offers "a confirmation by email" and nothing in the repository sends one. Words said after the address are dropped; words said before it are dropped only for the lead-ins in `parsers.py`.
- **Callback number.** Only North American numbers are accepted, with no extension. `+63 917 123 4567`, `+44 20 7946 0958` and `415 555 0100 extension 22` are all refused as too long, the question is asked again and a caller who cannot answer is escalated.
- **Recording.** A caller who refuses the recording stops the audio only. The transcript is still saved and delivered to both receivers. Tidewater's tenant file has `recording: notice`: the caller is told "This call is recorded for the firm's records" and is not asked; whether that is allowed where a caller stands is not decided here.

## What the guard catches

- **Emergencies.** The input guard fires on a call to 911, the word `emergency`, `in danger`, someone bleeding, dying, unconscious or badly hurt, overdose, suicide, `not breathing`, and being attacked, followed or threatened. It does not fire on `he is having a heart attack`, `she is choking`, `he has a gun`, `he is going to kill me`, `he is hitting me` or `my husband is beating me right now`, and "my husband is hurt" and "someone is attacking me" fall through as well.
- **What the agent says.** The output guard is a keyword filter: `You should sue them` is replaced, but `Your case sounds strong`, `The other driver is clearly at fault`, `You definitely have grounds to sue`, `We will take your case` and `The firm will represent you` pass. The model also sees the caller's words as spoken.

## What happens to the call

- **After hours.** An escalation after hours ends the call at once with the tenant's after-hours line and a callback task. Nothing more is asked on that path, so a caller who has not yet given a number leaves nothing to call back.
- **A declined caller.** A caller the Tidewater rules decline (a state outside Florida) still hears "The team will review your details and be in touch", and a normal-priority follow-up task is created next to the outcome `declined`.
- **A failed store write.** `file_call` stores the call and then queues its delivery with no guard between them (`sipclerk/server.py`, `sipclerk/call.py`). A failing write ends the call but loses the record, the transcript and both deliveries; only a log line remains. This was read in the code and not run.

## What is protected, and how far

- **The audit chain.** Each line holds the hash of the one before it, with no key. An edited line or a deleted line in the middle is detected; deleting the last lines or emptying the file is not. Only `TenantStore.read` writes an audit line, and nothing calls it; the deliveries that carry the name, number and story write none.
- **What the receivers get.** The console payload carries the caller's name, number and story with digit runs and email addresses masked (the console masks names itself on arrival, as `docs/integration_run.txt` shows). The bridge payload carries name, phone, email and description in clear. The speech and language models run on the machine and receive no caller words from anywhere else.
- **Models.** `scripts/fetch_models.py` downloads the Kokoro files with no checksum and no pinned revision.

## What was run, and on what

- **Versions.** Every figure and the spoken suite come from Pipecat 1.6.0, numpy 2.2.6 and pydantic 2.12.5. `requirements.txt` now pins Pipecat 1.13.0, numpy 2.5.4, pydantic 2.14.1, pytest 9.1.1 and pytest-asyncio 1.4.0 (a Dependabot merge made after those runs). CI runs the unit and pipeline tests on the new pins and passes; the spoken suite and the latency, token and call figures were not rerun on them.
- **Cached and rendered turns.** The 125 turns counted as "cached, no model" come from a flag that never marked a turn as rendered on the spot, so that bucket may include a read-back that was rendered when it was needed. The 151 ms median is read with that in mind.
- **The test run.** The suite can stall at exit on a loaded machine (stop it with Ctrl-C and run it again). The spoken suite rewrites `docs/voice_suite_run.json`, which is the source of the latency table.
