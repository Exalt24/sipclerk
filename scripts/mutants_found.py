"""Mutants for defects the voice runs and the property tests found, and for the processor and model fences. Each is pinned.

One more was written and dropped as equivalent: letting the model be asked after the plain parser already succeeded changes nothing, because
the branches below it test the parser's failure reasons, which a success never carries."""
from mutants import FAST, Mutant

DE = "sipclerk/delivery.py"
PROC = "sipclerk/voice/processors.py"
LLM = "sipclerk/voice/llm.py"
MA = "sipclerk/intake/machine.py"


def U(id, file, old, new, desc):
    return Mutant(id, file, old, new, list(FAST), desc, "replace", "unit")


FOUND_MUTANTS = [
    U("delivery-unqualified-record", DE, 'qualification = record["qualification"].get("result", "not_evaluated")', 'qualification = record["qualification"]["result"]',
      "a call that ended before it was qualified crashes the delivery builder"),
    U("proc-submit-error", PROC, '        except Exception as e:\n            log.exception("submit failed")', '        except ZeroDivisionError as e:\n            log.exception("submit failed")',
      "a failing write stops the call from ending"),
    U("proc-transfer-error", PROC, "                    except Exception as e:  # a transfer that blows up", "                    except ZeroDivisionError as e:  # a transfer that blows up",
      "a transfer that raises leaves the caller on a dead line"),
    U("proc-junk-filter", PROC, '(text.lower().strip(" .!?,") in JUNK_TRANSCRIPTS and t.seconds < 1.5)', "False",
      "a phantom word Whisper invents on noise is taken as the caller's answer"),
    U("proc-no-speech", PROC, "if (not text or t.no_speech_prob > 0.7 or", "if (not text or False or", "a transcript Whisper doubts is speech is kept"),
    U("proc-backchannel", PROC, 'if text.lower().strip(" .!?,") in BACKCHANNELS and m.state == State.ASK:', "if False:", "'hold on' counts as an invalid answer"),
    U("proc-guard-before-model", PROC, '            if verdict.action != "allow":', "            if False:", "the model sees an utterance the guard blocked"),
    U("llm-outage", LLM, "        except (LlmUnavailable, ValueError):\n            return None\n        return area if", "        except ValueError:\n            return None\n        return area if",
      "a model outage escapes as an exception"),
    U("llm-area-list", LLM, 'return area if area in [*area_ids, "other"] else None', "return area", "an area the firm does not list is accepted from the model"),
    U("llm-timeout", LLM, 'r = await asyncio.wait_for(self._client.post("/api/chat", json=body), self.timeout_s)', 'r = await self._client.post("/api/chat", json=body)',
      "a slow model blocks the call"),
    U("llm-ack-length", LLM, "return out if 3 <= len(out.split()) <= 25 else None", "return out", "a long or empty acknowledgement is spoken"),
    U("machine-endpointing-by-type", MA, 'ENDPOINT_BY_TYPE = {"text": 1.2, "phone": 1.0}', "ENDPOINT_BY_TYPE = {}", "a story or a phone number is cut off at the short pause"),
    U("proc-endpointing-follows-machine", PROC, "        if want != self.stop_secs:", "        if False:", "the detector never follows the question being asked"),
    U("machine-partial-forgotten", MA, '        fs.partial = combined\n        self._log("phone_partial"', '        fs.partial = ""\n        self._log("phone_partial"',
      "digits heard before a pause are forgotten"),
    U("machine-partial-limit", MA, "        if fs.partial_rounds > self.MAX_PARTIAL_ROUNDS:", "        if False:", "partial digits can be asked for forever"),
    U("machine-partial-keypad", MA, 'if q.type == "phone" and source == "spoken":', 'if q.type == "phone":', "a short keypad entry is completed by speech"),
]
