"""The voice regression suite: spoken WAV scripts replayed over real SIP and RTP through the real pipeline.

Run it with `python -m pytest -m voice` (about 20 minutes: every call is in real time). It needs the local
models (`python scripts/fetch_models.py`), the clips (`python scripts/make_wavs.py`) and Ollama with the model
named in sipclerk/voice/llm.py. Each assertion reads the call record the line saved, never the audio alone.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from sipclerk import guard
from sipclerk.harness import percentiles
from sipclerk.store import AccessDenied, Actor
from voice_support import Suite

pytestmark = pytest.mark.voice
ROOT = Path(__file__).resolve().parent.parent

NAME, PHONE = "Jordan Mitchell", "+14155550123"


@pytest.fixture(scope="module")
def suite():
    s = Suite().start()
    yield s
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "voice_suite_run.json").write_text(json.dumps(summary(s), indent=2), encoding="utf-8")
    s.stop()


def pct(values, p):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo))


def summary(s) -> dict:
    """Numbers from this run only: latency measured at the caller, and what each call cost in tokens and seconds."""
    onsets = s.all_onsets()
    agent = [m for _, res in s.cache.values() if res for m in res.record["latency_agent_side_ms"]]
    cached = [m["vad_stop_to_audio"] for m in agent if m["tts_cached"] and not m["llm_used"]]
    rendered = [m["vad_stop_to_audio"] for m in agent if not m["tts_cached"]]
    with_llm = [m["vad_stop_to_audio"] for m in agent if m["llm_used"]]
    calls = [res.record for _, res in s.cache.values() if res]
    full = [r for r in calls if r["outcome"].startswith("intake_")]

    def mean(rows, key):
        return round(sum(key(r) for r in rows) / len(rows), 2) if rows else None

    return {
        "scenarios_run": len(s.cache),
        "caller_side_ms": {"n": len(onsets), "p50": pct(onsets, 0.5), "p95": pct(onsets, 0.95), "max": round(max(onsets)) if onsets else None},
        "agent_side_vad_stop_to_first_audio_ms": {
            "cached_prompt_no_model": {"n": len(cached), "p50": pct(cached, 0.5), "p95": pct(cached, 0.95)},
            "a_sentence_rendered_on_the_spot": {"n": len(rendered), "p50": pct(rendered, 0.5), "p95": pct(rendered, 0.95)},
            "language_model_in_the_turn": {"n": len(with_llm), "p50": pct(with_llm, 0.5), "p95": pct(with_llm, 0.95)}},
        "per_completed_intake": {
            "calls": len(full),
            "call_seconds": mean(full, lambda r: r["duration_s"]),
            "llm_calls": mean(full, lambda r: r["usage"]["llm"]["calls"]),
            "llm_prompt_tokens": mean(full, lambda r: r["usage"]["llm"]["prompt_tokens"]),
            "llm_completion_tokens": mean(full, lambda r: r["usage"]["llm"]["completion_tokens"]),
            "llm_seconds": mean(full, lambda r: r["usage"]["llm"]["seconds"]),
            "stt_calls": mean(full, lambda r: r["usage"]["stt_calls"]),
            "stt_compute_seconds": mean(full, lambda r: r["usage"]["stt_audio_s"]),
            "agent_speech_seconds": mean(full, lambda r: r["usage"]["agent_audio_s"]),
            "caller_audio_seconds": mean(full, lambda r: r["usage"]["caller_audio_s"])},
        "tts_sentence_cache": {"hits": s.rig.models.cache_hits, "misses": s.rig.models.cache_misses},
        "calls": {n: {"wall_s": s.wall_s.get(n)} for n in s.cache},
    }


def rec(suite, name):
    played, result = suite.get(name)
    assert played.status == 200, f"{name}: the INVITE was answered with {played.status}"
    assert result is not None, f"{name}: the line saved no call record"
    return played, result.record, result


def values(record):
    return {k: v["value"] for k, v in record["fields"].items()}


def agent_lines(result):
    return [t["text"] for t in result.transcript if t["speaker"] == "AGENT"]


def kinds(record):
    return [e["kind"] for e in record["events"]]


# ----------------------------------------------------------------------------------------------- intake


def test_normal_intake_is_complete_and_accurate(suite):
    played, r, result = rec(suite, "normal_intake")
    v = values(r)
    assert r["outcome"] == "intake_complete" and r["qualification"]["result"] == "qualified"
    assert (v["full_name"], v["callback_number"], v["practice_area"], v["existing_client"]) == (NAME, PHONE, "personal_injury", False)
    assert v["incident_date"] == "2026-10-13" and "red light" in v["description"]
    assert r["fields"]["full_name"]["confirmed"] and r["fields"]["callback_number"]["confirmed"]
    assert played.no_response == 0 and played.ended_by_agent and r["ended_by"] == "agent:complete"
    assert r["codec"] == "PCMU" and r["recording"] is True and r["config_version"] == "2026.10.3" and len(r["config_hash"]) == 64


def test_the_record_is_stored_encrypted_and_readable_only_through_the_audited_path(suite):
    _, r, result = rec(suite, "normal_intake")
    store = suite.rig.store
    staff, lawyer = Actor("sam", "ashgrovepell", "intake_staff"), Actor("lee", "ashgrovepell", "attorney")
    assert store.read(staff, "ashgrovepell", r["call_id"], "record")["call_id"] == r["call_id"]
    assert store.read(staff, "ashgrovepell", r["call_id"], "transcript")[-1]["speaker"] == "AGENT"
    assert len(store.read(staff, "ashgrovepell", r["call_id"], "summary")) > 20
    assert store.read(lawyer, "ashgrovepell", r["call_id"], "recording")[:4] == b"RIFF"
    with pytest.raises(AccessDenied):
        store.read(staff, "ashgrovepell", r["call_id"], "recording")
    with pytest.raises(AccessDenied):
        store.read(Actor("eve", "tidewaterquint", "attorney"), "ashgrovepell", r["call_id"], "record")
    assert store.verify_audit("ashgrovepell") and sum(1 for x in store.audit_lines("ashgrovepell") if x["action"] == "read") >= 6
    assert b"Jordan" not in b"".join(p.read_bytes() for p in (store.root / "tenants" / "ashgrovepell" / "calls" / r["call_id"]).glob("*.enc"))


def test_second_firm_runs_its_own_rules(suite):
    _, r, result = rec(suite, "normal_intake_second_firm")
    v = values(r)
    assert r["tenant_id"] == "tidewaterquint" and r["config_version"] == "2026.10.6"
    assert (v["full_name"], v["practice_area"], v["state"], v["urgent_deadline"]) == ("Dana Whitfield", "family_law", "Florida", True)
    assert v["callback_number"] == "+17865550147" and r["fields"]["email"]["skipped"]
    assert r["outcome"] == "intake_priority" and r["qualification"]["result"] == "priority" and r["recording"] is True
    assert result.transcript[0]["text"].startswith("Tidewater Quint Law")


def test_two_firms_at_once_share_nothing(suite):
    (pa, ra), (pb, rb) = suite.together("normal_intake", "normal_intake_second_firm")
    a, b = ra.record, rb.record
    assert a["tenant_id"] == "ashgrovepell" and b["tenant_id"] == "tidewaterquint"
    assert a["outcome"] == "intake_complete" and b["outcome"] == "intake_priority"
    cfg_a, cfg_b = (suite.rig.registry.by_id(t).config for t in ("ashgrovepell", "tidewaterquint"))
    lines_a, lines_b = " ".join(agent_lines(ra)), " ".join(agent_lines(rb))
    for q in cfg_b.questions:
        assert q.prompt not in lines_a
    for q in cfg_a.questions:
        assert q.prompt not in lines_b
    assert "Ashgrove" not in lines_b and "Tidewater" not in lines_a
    assert a["did"] != b["did"] and a["config_hash"] != b["config_hash"]
    assert suite.rig.store.list_calls("ashgrovepell") != suite.rig.store.list_calls("tidewaterquint")
    assert a["call_id"] not in suite.rig.store.list_calls("tidewaterquint") and b["call_id"] not in suite.rig.store.list_calls("ashgrovepell")
    assert "Dana" not in json.dumps(a) and "Jordan" not in json.dumps(b)


def test_after_hours_says_so_and_still_takes_the_intake(suite):
    _, r, result = rec(suite, "after_hours")
    assert r["in_hours"] is False and r["outcome"] == "intake_complete"
    assert suite.rig.registry.by_id("ashgrovepell").config.after_hours_message in agent_lines(result)


# ----------------------------------------------------------------------------------------------- conversation behaviour


def test_barge_in_stops_the_agent_and_the_number_is_still_captured(suite):
    played, r, result = rec(suite, "barge_in")
    assert r["barge_ins"] >= 1 and "barge_in" in kinds(r)
    assert played.barge_stop_ms is not None and played.barge_stop_ms < 1200, played.barge_stop_ms
    assert values(r)["callback_number"] == PHONE and r["outcome"] == "intake_complete"


def test_long_silence_gets_a_reprompt_and_the_call_carries_on(suite):
    _, r, result = rec(suite, "long_silence")
    assert "silence" in kinds(r) and sum("records this call" in t for t in agent_lines(result)) >= 2
    assert r["outcome"] == "intake_complete" and values(r)["full_name"] == NAME


def test_endless_silence_ends_the_call_with_a_callback_task(suite):
    played, r, result = rec(suite, "abandoned_silence")
    assert r["outcome"] == "abandoned_silent" and r["tasks"][-1]["kind"] == "callback" and r["ended_by"] == "agent:silence"
    assert [e["kind"] for e in r["events"]].count("silence") == 3 and played.ended_by_agent


def test_a_noisy_line_still_gives_an_accurate_record(suite):
    played, r, result = rec(suite, "noisy_line")
    v = values(r)
    assert r["outcome"] == "intake_complete", agent_lines(result)
    assert (v["full_name"], v["callback_number"], v["practice_area"], v["incident_date"]) == (NAME, PHONE, "personal_injury", "2026-10-13")


def test_a_caller_correction_is_taken_and_read_back(suite):
    _, r, result = rec(suite, "caller_correction")
    assert "caller_correction" in kinds(r) and values(r)["full_name"] == NAME and r["outcome"] == "intake_complete"


def test_invalid_answers_are_asked_again_until_they_are_valid(suite):
    _, r, result = rec(suite, "invalid_answers")
    reasons = [e.get("reason") for e in r["events"] if e["kind"] == "invalid_answer"]
    assert {"need_last_name", "in_the_future"} <= set(reasons) and "phone_partial" in kinds(r), (reasons, kinds(r))
    v = values(r)
    assert (v["full_name"], v["callback_number"], v["incident_date"]) == (NAME, PHONE, "2026-10-13") and r["outcome"] == "intake_complete"


def test_dtmf_digits_fill_the_phone_field(suite):
    _, r, result = rec(suite, "dtmf_phone_number")
    f = r["fields"]["callback_number"]
    assert f["value"] == PHONE and f["source"] == "keypad" and f["confirmed"] and r["outcome"] == "intake_complete"
    assert all(e.get("digit") in (None, "x", "0") for e in r["events"])  # digits are never logged


# ----------------------------------------------------------------------------------------------- guard


def test_unsupported_practice_area_is_declined_politely_with_a_review_task(suite):
    played, r, result = rec(suite, "unsupported_practice_area")
    assert r["outcome"] == "declined_out_of_scope" and r["tasks"][0]["kind"] == "review_declined_caller"
    assert suite.rig.registry.by_id("ashgrovepell").config.guard.out_of_scope_message in agent_lines(result)
    assert not r["transfer"].get("attempted") and played.ended_by_agent


def test_a_request_for_legal_advice_is_declined_and_the_intake_continues(suite):
    _, r, result = rec(suite, "legal_advice_request")
    assert any(g["rule"] == "legal_advice_request" for g in r["guard_events"] if g["side"] == "input")
    assert any("cannot give legal advice" in t for t in agent_lines(result))
    assert r["outcome"] == "intake_complete" and values(r)["full_name"] == NAME


def test_a_repeated_request_for_advice_goes_to_a_person(suite):
    played, r, result = rec(suite, "legal_advice_repeated")
    assert r["escalation"] == "legal_advice_repeat" and r["transfer"]["result"] == "connected"


def test_no_agent_line_in_any_call_breaks_a_safety_rule(suite):
    for name in ("normal_intake", "legal_advice_request", "unsupported_practice_area", "provider_llm_error", "barge_in"):
        suite.get(name)
    violations = [(n, line) for n, (_, res) in suite.cache.items() if res for line in agent_lines(res) if not guard.check_output(line).ok]
    assert violations == []
    assert sum(len(res.record["guard_events"]) for _, res in suite.cache.values() if res and any(g["side"] == "output" for g in res.record["guard_events"])) == 0


# ----------------------------------------------------------------------------------------------- transfer


def test_warm_transfer_whispers_to_staff_then_bridges_audio_both_ways(suite):
    played, r, result = rec(suite, "existing_client_transfer")
    t = r["transfer"]
    assert r["escalation"] == "existing_client" and t["result"] == "connected" and t["attempted"] and r["outcome"] == "transferred"
    assert t["whisper"].startswith("Intake transfer for Ashgrove and Pell.") and "existing client" in t["whisper"]
    b = played.bridge
    assert b["connected"] and b["whisper_frames"] > 40, b
    assert b["staff_heard_caller_tone"] and b["caller_heard_staff_tone"], b
    assert r["ended_by"] in ("caller", "staff")


def test_a_busy_desk_phone_leaves_a_callback_and_an_honest_line(suite):
    _, r, result = rec(suite, "transfer_busy")
    assert r["transfer"]["result"] == "failed" and "486" in r["transfer"]["detail"]
    assert r["outcome"] == "callback_after_failed_transfer" and r["tasks"][-1]["kind"] == "callback"
    assert suite.rig.registry.by_id("ashgrovepell").config.transfer.unavailable in agent_lines(result)


def test_a_desk_phone_that_rings_out_is_cancelled_and_the_caller_is_not_left_hanging(suite):
    played, r, result = rec(suite, "transfer_no_answer")
    assert r["transfer"]["result"] == "failed" and r["transfer"]["ring_s"] >= 5.5 and r["outcome"] == "callback_after_failed_transfer"
    assert suite.rig.staff["ashgrovepell"].invites[-1].cancelled.is_set() and played.ended_by_agent


def test_pressing_zero_reaches_a_person(suite):
    played, r, result = rec(suite, "dtmf_zero_for_a_person")
    assert r["escalation"] == "dtmf_zero" and r["transfer"]["result"] == "connected" and r["tenant_id"] == "tidewaterquint"
    assert played.bridge["connected"]


# ----------------------------------------------------------------------------------------------- provider failures


def test_speech_to_text_timeout_is_a_missed_answer_not_a_dead_call(suite):
    _, r, result = rec(suite, "provider_stt_timeout")
    assert "stt_failure" in kinds(r) and r["outcome"] == "intake_complete" and values(r)["callback_number"] == PHONE


def test_language_model_outage_falls_back_to_plain_parsing_and_a_template_summary(suite):
    _, r, result = rec(suite, "provider_llm_error")
    assert r["usage"]["llm"]["failures"] >= 1 and r["outcome"] == "intake_complete" and values(r)["practice_area"] == "personal_injury"
    summary = suite.rig.store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", r["call_id"], "summary")
    assert summary.startswith("Jordan Mitchell called Ashgrove and Pell LLP")


def test_a_failing_webhook_receiver_never_touches_the_call_and_the_delivery_is_retried(suite):
    played, r, result = rec(suite, "provider_webhook_500")
    assert r["outcome"] == "intake_complete" and played.no_response == 0
    outbox = suite.rig.outbox
    end = time.monotonic() + 25
    while time.monotonic() < end and any(e.status == "pending" for e in outbox.entries.values()):
        time.sleep(0.5)
    mine = [e for e in outbox.entries.values() if e.key == r["idempotency_key"]]
    assert {e.target for e in mine} == {"bridge", "console"} and all(e.status == "delivered" for e in mine), [(e.target, e.status, e.history) for e in mine]
    assert max(e.attempts for e in mine) >= 3


def test_every_finished_intake_was_queued_to_both_receivers_once(suite):
    _, r, _ = rec(suite, "normal_intake")
    keys = [(e.target, e.key) for e in suite.rig.outbox.entries.values() if e.key == r["idempotency_key"]]
    assert sorted(keys) == [("bridge", r["idempotency_key"]), ("console", r["idempotency_key"])]


# ----------------------------------------------------------------------------------------------- numbers


def test_latency_of_the_first_audio_after_the_caller_stops_talking(suite):
    for n in ("normal_intake", "normal_intake_second_firm", "barge_in", "noisy_line", "invalid_answers"):
        suite.get(n)
    p = asyncio.run(percentiles(suite.all_onsets()))
    assert p["n"] >= 30 and p["p50"] < 1300 and p["p95"] < 2800, p
