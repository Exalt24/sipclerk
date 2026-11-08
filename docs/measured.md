# What was measured

Moved out of the [README](../README.md), which links here.

First audio after the caller stops talking is measured at the caller's side. It runs from the last RTP packet of the caller's sentence to the first loud RTP frame of the agent's reply, over every turn of the 20 spoken scenarios:

| | Turns | Median | 95th percentile |
|---|---|---|---|
| All turns, as the caller hears them | 141 | 619 ms | 1611 ms |
| Agent side, from the detector's end of speech to the first audio, no model in the turn | 125 | 151 ms | 267 ms |
| Agent side, the language model in the turn (the closing acknowledgement) | 14 | 572 ms | 1019 ms |

Most of the median is the silence that ends a turn (0.5 s for a yes or a name, 1.0 s for a number, 1.2 s for a story), not compute. Whisper transcribes an utterance in about 155 ms, which is roughly all of the agent side's 151 ms median.

Fixed prompts are rendered once and cached (370 sentences answered, 120 missed; two of the 141 turns have no agent-side figure). The 14 closing turns speak a sentence the model wrote, so their time includes rendering it. A real caller's first read-back is rendered on the spot too, a time that was not kept.

These figures were measured before the review round fixes. The replay after them did not save its numbers.

Per call, in the same earlier run, a completed intake took 80.4 s on the line (halved timeouts) and 1.08 model calls, without the staff summary call (nothing here is billed). The table is in [`PROOF.md`](PROOF.md).

The SIP leg, from [`sipp_run.json`](sipp_run.json): SIPp offered PCMA, PCMU and telephone-event. The line answered PCMU 8 kHz, 4,367 RTP packets arrived and eleven keypad events became the phone number. The call ended with the agent's BYE. The audio path, with numbers, is in [`codec_proof.txt`](codec_proof.txt).
