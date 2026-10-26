"""`he's bleeding`, `I'm bleeding` and `they're dying` did not fire the emergency rule: it listed `is` and `are` and no contraction, so a caller in the way most people speak was taken as an ordinary call. Found by the last independent reader of the repository."""
import itertools
from pathlib import Path

import pytest

from sipclerk import guard, tenants

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def cfg():
    lt = tenants.load_file(ROOT / "tenants" / "tidewaterquint.yaml")
    return getattr(lt, "config", lt)


# CLASS-TABLE: every subject with every way of saying "is", against every condition and a qualifier, in both apostrophes
SUBJECTS = {"he": ("is", "'s", "’s"), "she": ("is", "'s", "’s"), "my husband": ("is", "'s", "’s"), "my mother": ("is", "'s", "’s"),
            "someone": ("is", "'s", "’s"), "they": ("are", "'re", "’re"), "we": ("are", "'re", "’re"), "i": ("am", "'m", "’m")}
CONDITIONS = ("bleeding", "dying", "unconscious", "badly hurt", "seriously hurt")
LEADS = ("", "please help ", "oh no ")


def _said(subject, cop, condition, lead):
    return f"{lead}{subject}{cop if cop.startswith(('’', chr(39))) else ' ' + cop} {condition}"


def test_class_every_way_of_saying_someone_is_in_danger_fires(cfg):
    bad = []
    for (subject, cops), condition, lead in itertools.product(SUBJECTS.items(), CONDITIONS, LEADS):
        for cop in cops:
            said = _said(subject, cop, condition, lead)
            if guard.check_input(said, cfg).action != "emergency":
                bad.append(said)
    assert not bad, bad[:10]


@pytest.mark.parametrize("said", ["my back's hurt", "my neck is hurt", "he's hurt", "my husband's hurt", "he was bleeding", "she's been unconscious since the crash happened last week",
                                  "she was unconscious for a minute after the crash", "the paramedics said he was bleeding when they arrived"])
def test_an_injury_report_or_an_account_of_the_past_still_does_not_fire(cfg, said):
    assert guard.check_input(said, cfg).action != "emergency"
