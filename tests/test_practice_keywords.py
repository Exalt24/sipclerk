"""A practice area is never picked by a bare common word (output round 3, F6).

`My husband will not let me see our kids` was filed as estate planning because the tenant file listed the word `will`, and the matter went to the wrong attorney
with no read-back.
"""
import itertools

import pytest

from sipclerk import guard
from sipclerk.intake import parsers as p

# Words that carry no legal meaning on their own: auxiliaries, modals, pronouns, articles, prepositions and conjunctions. A keyword is never one of these alone.
FUNCTION_WORDS = {
    "will", "would", "can", "could", "may", "might", "must", "shall", "should", "do", "does", "did", "have", "has", "had", "is", "are", "was", "were", "be", "been",
    "am", "i", "me", "my", "we", "us", "our", "you", "your", "he", "him", "his", "she", "her", "it", "its", "they", "them", "their", "a", "an", "the", "and", "or", "but",
    "not", "no", "yes", "of", "to", "in", "on", "at", "by", "for", "with", "from", "up", "out", "as", "if", "so", "that", "this", "what", "who", "how", "when", "go", "get",
}


def _all_keywords(registry):
    for firm in registry.all:
        cfg = firm.config
        for area in cfg.practice_areas:
            for kw in area.keywords:
                yield cfg.tenant_id, area.id, kw
        for kw in cfg.guard.out_of_scope_keywords:
            yield cfg.tenant_id, "out_of_scope", kw


# CLASS-TABLE: every keyword of every tenant (practice areas and out of scope) against the closed list of function words
def test_no_keyword_is_a_bare_function_word(registry):
    seen = 0
    for tenant, area, kw in _all_keywords(registry):
        assert kw.strip().lower() not in FUNCTION_WORDS, (tenant, area, kw)
        assert kw == kw.strip(), (tenant, area, kw)
        seen += 1
    assert seen > 50


# CLASS-TABLE: every keyword of every practice area, said in a sentence of its own, reads as its own area (and no other)
def test_every_keyword_still_finds_its_area(registry):
    for firm in registry.all:
        cfg = firm.config
        for area in cfg.practice_areas:
            for kw in area.keywords:
                for template in ("it is about {}", "I need help, {}", "{} is the reason I am calling"):
                    r = p.parse_area(template.format(kw), cfg.practice_areas)
                    others = [a.id for a in cfg.practice_areas if a.id != area.id and kw in a.keywords]
                    if others:
                        continue
                    assert r.ok and r.value == area.id, (cfg.tenant_id, kw, template, r)


MODAL_SUBJECTS = ["my husband", "my wife", "he", "she", "they", "my ex", "the other driver", "my sister", "we", "I", "it", "the landlord", "my neighbour"]
MODAL_TAILS = ["will not let me see our kids", "will pay me back on Friday", "will call you tomorrow", "will take a while", "will not return my calls",
               "will be there at five", "will come in on Monday", "will say anything", "will never agree to that", "will not talk to me",
               "will be late", "will not answer the phone"]


# CLASS-TABLE: every subject x every sentence with the modal "will", against every area of both firms: the word alone picks nothing
@pytest.mark.parametrize("subject,tail", list(itertools.product(MODAL_SUBJECTS, MODAL_TAILS)))
def test_the_modal_will_picks_no_area(registry, subject, tail):
    for firm in registry.all:
        r = p.parse_area(f"{subject} {tail}", firm.config.practice_areas)
        assert not r.ok and r.reason == "no_match", (firm.config.tenant_id, subject, tail, r)


ESTATE_SENTENCES = ["I need to make a will", "I want to update my will", "my dad passed away and there is a will", "I have a question about the will",
                    "his will leaves the house to my brother", "her will was never signed", "their will names the wrong person", "our will needs changing",
                    "my last will and testament", "I want a living will", "I need help with my father's will", "my mother's will is being contested",
                    "my husband's will left everything to his brother", "my wife's will", "my mom's will", "my dad's will", "a will for me and my spouse",
                    "can you help with a will"]


@pytest.mark.parametrize("said", ESTATE_SENTENCES)
def test_a_caller_who_asks_about_a_will_is_still_read_as_estate_planning(tidewater, said):
    r = p.parse_area(said, tidewater.config.practice_areas)
    assert r.ok and r.value == "estate_planning" or (r.reason == "ambiguous" and "estate_planning" in r.detail), (said, r)


def test_the_round_sentence_goes_to_the_model_or_a_question_not_to_estate_planning(tidewater):
    r = p.parse_area("My husband will not let me see our kids", tidewater.config.practice_areas)
    assert not r.ok and r.reason == "no_match"


def test_the_guard_does_not_read_will_as_an_out_of_scope_word(registry):
    for firm in registry.all:
        assert guard.check_input("my husband will not let me see our kids", firm.config, check_scope=True).action == "allow"
