"""Replay of the blind corpus of output round 3 (59 cases, 44 that must pass and 15 that must fail) against the real code.

Every row of `tests/corpus/sipclerk_corpus_blind_3.jsonl` is replayed through the function of the same id in `tests/blind3_cases.py`. A row that expects `pass` is an
ordinary input the code must take; a row that expects `fail` is an input the code must refuse, decline or hand to a person. Four rows are known limits: their
test pins the recorded behaviour, so a change to it is seen (and then the limit is either gone or has moved). All 59 rows replay offline.
"""
import json
from pathlib import Path

import pytest

import blind3_cases as bc

CORPUS = Path(__file__).resolve().parent / "corpus" / "sipclerk_corpus_blind_3.jsonl"
ROWS = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]

# row number (1 based) -> why the recorded behaviour is kept, from the triage of output round 3
KNOWN_LIMITS = {
    25: "F7: `No problem` to the recording question turns recording off, the safe direction (nothing is recorded that the caller may not have agreed to)",
    26: "F7: `No, that's fine` to the recording question turns recording off, the same limit",
    50: "F8: the written summary is on the console record; the bridge payload carries every field but an empty summary",
    51: "F9: a surname holding the skip word `pass` skips the email, and the caller is asked for the address again at the end",
}
PAYLOAD_ISO = {12: "2025-03-01", 13: "2026-08-01", 14: "2026-03-03", 15: "2026-09-05", 16: "2026-10-13", 17: "2026-10-23", 18: "2026-03-03"}
EMAILS = {5: "jordan.reyes@gmail.com", 6: "jordan.reyes@gmail.com", 7: "jordanreyes@gmail.com", 8: "jordan.reyes@gmail.com", 9: "jordan.reyes@gmail.com"}
PHONE = "+14155550123"


def _full(r):  # a complete intake: the outcome and no field left empty
    return r["outcome"] == "intake_complete" and all(v not in (None, "") for v in r["fields"].values())


# row number -> what the replayed result must show (a function of the result)
CHECKS = {
    1: lambda r: r["is_open"] is True and "<StartTransfer>" in r["said"],
    2: lambda r: r["is_open"] is False,
    3: lambda r: r["is_open"] is True,
    4: lambda r: r["is_open"] is True,
    **{n: (lambda e: lambda r: r["email"] == e and not r["skipped"])(e) for n, e in EMAILS.items()},
    10: lambda r: r["email"] is None and r["skipped"],
    11: lambda r: r["email"] is None and r["skipped"],
    **{n: (lambda iso: lambda r: r["incident_date"] == iso)(iso) for n, iso in PAYLOAD_ISO.items()},
    19: lambda r: r["pending"] == PHONE and r["state"] == "confirm",
    20: lambda r: r["state"] == "confirm" and r["pending"] == PHONE and r["keypad_len"] == 0 and r["retries"] == 0,
    21: lambda r: r["pending"] == PHONE,
    22: lambda r: r["practice_area"] in (None, "family_law"),
    23: lambda r: r["practice_area"] == "family_law",
    24: lambda r: r["practice_area"] == "personal_injury",
    27: lambda r: r["recording"] is True,
    28: lambda r: r["escalation"] == "" and r["outcome"] != "callback_requested",
    29: lambda r: r["escalation"] == "" and r["description"] is not None,
    30: lambda r: r["escalation"] == "" and r["description"] is not None,
    31: lambda r: r["escalation"] == "existing_client" and "<StartTransfer>" in r["said"],
    32: lambda r: r["escalation"] == "emergency",
    33: lambda r: r["outcome"] == "intake_complete" and r["escalation"] == "",
    34: lambda r: r["state"] == "ask" and r["escalation"] == "" and r["said"][0].startswith("I cannot give legal advice"),
    35: lambda r: r["outcome"] == "declined_out_of_scope",
    36: lambda r: r["outcome"] == "declined_out_of_scope",
    37: lambda r: _full(r),
    38: lambda r: _full(r),
    39: lambda r: r["parsed"]["ok"] and r["parsed"]["value"] == "José García",
    40: lambda r: r["parsed"]["ok"] and r["parsed"]["value"] == "Sarah O'Connor",
    41: lambda r: r["full_name"] == "Jordan Rees",
    42: lambda r: r["parsed"]["value"] == PHONE,
    43: lambda r: r["parsed"]["value"] == PHONE,
    44: lambda r: r["tenant"] == "ashgrovepell" and r["codec"][0] == "PCMU" and r["dtmf_pt"] == 101 and r["caller"] == "+14155550142",
    45: lambda r: r["tenant"] == "tidewaterquint",
    46: lambda r: r["tenant"] is None,
    47: lambda r: r["caller"] == "" and r["withheld"] is True,
    48: lambda r: r["caller"] == "+14155550142",
    49: lambda r: r["verdict"] == "blocked_caller",
    52: lambda r: r["codec"] is None,
    53: lambda r: "call-id" in r["error"].lower(),
    54: lambda r: r["parsed"]["reason"] == "not_a_phone_number",
    55: lambda r: r["parsed"]["reason"] == "in_the_future",
    56: lambda r: r["parsed"]["reason"] == "not_a_name",
    57: lambda r: r["tenant"] is None,
    58: lambda r: r["verdict"]["ok"] is False and r["verdict"]["text"].startswith("I want to be careful"),
    59: lambda r: r["escalation"] == "legal_advice_repeat",
}


def _replayed(row):
    return json.loads(json.dumps(bc.CASES[row["id"]](), default=str))


def test_the_corpus_is_the_59_cases_of_the_round():
    assert len(ROWS) == 59 and sum(r["expect"] == "pass" for r in ROWS) == 44 and sum(r["expect"] == "fail" for r in ROWS) == 15
    assert len({r["id"] for r in ROWS}) == 59


def test_every_row_has_a_replay_and_a_check_or_a_pinned_limit():
    for n, row in enumerate(ROWS, 1):
        assert row["id"] in bc.CASES, (n, row["id"])
        assert (n in CHECKS) != (n in KNOWN_LIMITS), n
        assert row["source"] in ("real", "invented") and len(row["why"]) >= 10, n
    assert set(CHECKS) | set(KNOWN_LIMITS) == set(range(1, 60))


@pytest.mark.parametrize("n", sorted(CHECKS))
def test_the_row_behaves_as_it_must(n):
    row = ROWS[n - 1]
    result = _replayed(row)
    assert CHECKS[n](result), (n, row["id"], row["expect"], row["expected"], result)


@pytest.mark.parametrize("n", sorted(KNOWN_LIMITS))
def test_a_known_limit_still_behaves_as_recorded(n):
    row = ROWS[n - 1]
    assert _replayed(row) == json.loads(row["actual"]), (n, KNOWN_LIMITS[n])
