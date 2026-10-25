"""Names are read as written in any language: the letters a caller's name is made of are never dropped, read back, stored or delivered as something else.

The first parser turned every letter outside A to Z into a space (Munoz with a tilde became `Mu Oz`, `Garcia` with an accent was refused twice).
The ASCII behaviour is pinned against a frozen copy of that parser, so widening it cannot change what an ASCII name does."""
import json
import random
import re
import unicodedata

import pytest

from conftest import IN_HOURS
from sipclerk import delivery, guard
from sipclerk.intake import parsers as p
from sipclerk.intake.machine import IntakeMachine, Say
from sipclerk.store import Actor, TenantStore
from sipclerk.tenants import PracticeArea

# 30 real names from several languages, as a person would say them: (name as said, how it must come out)
NAMES = [
    "José García", "María Peña", "Daniel Muñoz", "Zoë Brontë", "François Dupont", "Renée Lefèvre", "Jürgen Müller", "Björn Lindström",
    "Åsa Håkansson", "Søren Kierkegaard", "Zoltán Szűcs", "Łukasz Wójcik", "Antonín Dvořák", "Cristina Fernández", "João Gonçalves", "Mehmet Çelik",
    "İbrahim Yılmaz", "Nguyễn Thị Hương", "Eilís Ní Dhomhnaill", "Sofía Rodríguez", "Chloé Lefebvre", "Αλέξανδρος Παπαδόπουλος", "Анна Иванова",
    "Žofie Nováková", "Ørjan Sørensen", "Siobhán Ó Briain", "Joaquín Guzmán", "Thérèse Martin", "Michał Zieliński", "Ádám Tóth",
]
# unchanged by the capitalisation rule that already applied to ASCII names (first letter up, the rest down, `O'` and `Mc` special)
NAMES_WITH_PUNCTUATION = [("Seán O'Brien", "Seán O'Brien"), ("Seán O’Brien", "Seán O'Brien"), ("Dr. Álvaro Núñez", "Dr Álvaro Núñez"), ("Jean-Luc Garçon", "Jean-luc Garçon")]


# The stop words as they were: the four that are also parts of real names (test_names_stopwords.py) were part of this set, and the frozen parser keeps them
# so the comparison below still holds for every input that does not say one of them capitalised.
OLD_NAME_STOP = p.NAME_STOP | p.NAME_AMBIGUOUS


def old_parse_name(text: str):
    """The parser as it was before: A to Z only. Returns (ok, value, reason)."""
    raw = re.sub(r"[^A-Za-z'\-\s]", " ", text.replace("’", "'"))
    raw = p.NAME_LEAD.sub("", raw.strip())
    tokens = [t.strip("-'") for t in raw.split() if t.strip("-'")]
    if not tokens:
        return (False, None, "empty")
    if len(tokens) > 5 or any(t.lower() in OLD_NAME_STOP for t in tokens):
        return (False, None, "not_a_name")
    if re.search(r"\d", text) and not re.search(r"[A-Za-z]{2}", text):
        return (False, None, "not_a_name")
    name = " ".join(t[:1].upper() + t[1:].lower() if not re.match(r"^(mc|o')", t.lower()) else t[:1].upper() + t[1:2] + t[2:3].upper() + t[3:].lower() for t in tokens)
    if len(tokens) < 2:
        return (False, name, "need_last_name")
    return (True, name, "")


@pytest.mark.parametrize("name", NAMES)
def test_a_real_name_comes_out_exactly_as_written(name):
    r = p.parse_name(name)
    assert r.ok and r.value == name == unicodedata.normalize("NFC", name), (name, r)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("lead", ["My name is {}", "it's {}", "this is {}", "Hi, I am {}"])
def test_with_a_lead_in_and_in_decomposed_form_the_name_is_the_same_composed_name(name, lead):
    said = lead.format(unicodedata.normalize("NFD", name))
    r = p.parse_name(said)
    assert r.ok and r.value == name and unicodedata.is_normalized("NFC", r.value), (said, r)


@pytest.mark.parametrize("said,expected", NAMES_WITH_PUNCTUATION)
def test_apostrophes_hyphens_and_periods_work_as_they_did(said, expected):
    assert p.parse_name(said).value == expected


def test_a_first_name_alone_is_still_asked_for_a_last_name_and_digits_and_noise_are_still_refused():
    assert p.parse_name("María").reason == "need_last_name" and p.parse_name("María").value == "María"
    for bad in ("1234 5678", "yes", "I was in a car accident last week", "", "¿¡?!", "12 ñ"):
        assert not p.parse_name(bad).ok, bad


ASCII_PIECES = ["Jordan", "Reyes", "mary-jane", "O'Neil", "o'brien", "McDonald", "mcgee", "van", "der", "Berg", "Dr.", "Smith-Jones", "my name is", "it's", "this is",
                "yes", "no", "case", "lawyer", "7", "415-555", "a", "I", "  ", "!", "?", ",", "'", "-", "x" * 12, "RYAN", "pat", "ANNE MARIE"]


def test_class_every_ascii_input_reads_exactly_as_the_old_parser_read_it():
    """4,000 seeded random ASCII utterances: the new parser agrees with the frozen old one on ok, value and reason."""
    rnd = random.Random(2026)
    for _ in range(4000):
        text = " ".join(rnd.choice(ASCII_PIECES) for _ in range(rnd.randint(0, 7)))
        new = p.parse_name(text)
        assert (new.ok, new.value, new.reason) == old_parse_name(text), repr(text)


def test_class_a_random_accented_name_is_never_reduced_to_ascii_pieces():
    """For every letter the old parser threw away: put it in a name and the name keeps it."""
    letters = [c for c in "áàâäãåāăąçćčďđéèêëēėęěğíìîïīįıłńňñóòôöõøōőřśšşťúùûüūůűųýÿźżž" + "ÁÀÂÄÃÅĀĂĄÇĆČĎĐÉÈÊËĒĖĘĚĞÍÌÎÏĪĮİŁŃŇÑÓÒÔÖÕØŌŐŘŚŠŞŤÚÙÛÜŪŮŰŲÝŸŹŻŽ"]
    for c in letters:
        first, last = f"A{c.lower()}n", f"B{c.lower()}rk"
        r = p.parse_name(f"{first} {last}")
        assert r.ok and r.value == f"{first} {last}", (c, r)


# ---- nothing after the parser drops them either

TRANSCRIPT_ANSWERS = ["yes", "José García", "yes"]


def drive(tenant, utterances):
    m = IntakeMachine(tenant, "c1", "+14155550142", IN_HOURS)
    m.begin()
    said = []
    for u in utterances:
        said += [a.text for a in m.handle_text(u) if isinstance(a, Say)]
    return m, said


def test_the_read_back_the_confirmation_the_record_and_the_correction_keep_the_diacritics(tidewater):
    m, said = drive(tidewater, ["José García"])
    assert "I have José García. Is that right?" in said
    m, said = drive(tidewater, ["Jose Garcia", "no, it is José García"])
    assert any("José García" in s for s in said) and m.pending == "José García"
    m, _ = drive(tidewater, ["José García", "yes"])
    assert m.fields["full_name"].value == "José García" and m.fields["full_name"].confirmed


def test_the_spoken_line_the_whisper_the_stored_record_and_both_deliveries_carry_the_name(tidewater, registry, tmp_path):
    m, said = drive(tidewater, ["José García", "yes"])
    assert guard.check_output("I have José García. Is that right?").text == "I have José García. Is that right?"  # the output guard does not rewrite it
    rec = m.record()
    # stored encrypted and read back
    store = TenantStore(tmp_path, registry, bytes(range(32)) * 2, clock=lambda: IN_HOURS)
    store.save_call("tidewaterquint", "c1", rec, [], "José García called.", None)
    back = store.read(Actor("sam", "tidewaterquint", tidewater.config.staff_roles[0]), "tidewaterquint", "c1", "record")
    assert back["fields"]["full_name"]["value"] == "José García"
    # both payloads, as they cross the wire
    rec["qualification"] = rec["qualification"] or {"result": "qualified"}
    bridge = json.loads(json.dumps(delivery.build_bridge_payload(rec, "José García called."), separators=(",", ":")))
    console = json.loads(json.dumps(delivery.build_console_payload(rec, [{"speaker": "AGENT", "text": "I have José García. Is that right?"}], 30, "Firm"), separators=(",", ":")))
    assert bridge["data"]["client"]["name"] == "José García" and console["patientName"] == "José García"
    assert "José García" in console["transcript"][0]["text"]


# ---- the sweep: the other readers of text

def test_other_parsers_no_longer_split_words_at_an_accented_letter():
    assert p._norm("Señor Núñez, café!") == "señor núñez café"
    assert p.parse_yesno("no, it's José García").remainder == "josé garcía"
    area = PracticeArea(id="family", label="Family", keywords=["cuñado", "divorce"])
    assert p.parse_area("mi cuñado y yo", [area]).value == "family"  # a keyword a firm wrote with an accent can match at last
    assert p.parse_text("Анна пошла домой", 3).ok and not p.parse_text("ñ", 3).ok
    assert p.parse_email("jose dot garcia at example dot com").value == "jose.garcia@example.com"  # spoken addresses are ASCII on purpose
