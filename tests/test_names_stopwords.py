"""A word that is also a filler is not a veto on a real name.

`An Nguyen`, `Thi An Tran`, `Daniel To`, `Kevin Case` and `Mary Hurt` were refused as not a name, twice, and then the caller was transferred to a person,
because `an`, `to`, `case` and `hurt` were on the list of words that are an answer or a filler. Those four words are read as part of a name when they are
heard capitalised next to a word that is only a name, or when the model already extracted the text as a name. Everything that is only an answer or a
filler (yes, no, hello, okay, a request, a story) stays refused."""
import itertools

import pytest

from conftest import IN_HOURS
from sipclerk.intake import parsers as p
from sipclerk.intake.machine import IntakeMachine, Say
from sipclerk.voice import processors as vp
from sipclerk.voice.llm import Llm
from test_processors import make_ctx

REAL = [("My name is An Nguyen", "An Nguyen"), ("It's Thi An Tran", "Thi An Tran"), ("Kevin Case", "Kevin Case"), ("Mary Hurt", "Mary Hurt"),
        ("Daniel To", "Daniel To"), ("An Nguyen", "An Nguyen"), ("Hi, I am An Tran", "An Tran"), ("this is Linh To", "Linh To"), ("Ana Case", "Ana Case"),
        ("Will Hope", "Will Hope"), ("Jordan Reyes", "Jordan Reyes")]

NOT_A_NAME = ["yes", "no", "hello", "okay", "ok", "hi", "sorry", "please", "skip", "yes please", "no thanks", "hello there", "I need a lawyer", "I want to hurt",
              "I have a case", "my case", "a case", "an", "to", "case", "hurt", "to hurt", "An Case", "To Hurt", "what is this", "I was in a car accident",
              "can you help me", "talk to someone", "go to hell"]

NAME_WORDS = ["Kevin", "Mary", "Daniel", "Linh", "Ana", "Reyes", "Nguyen", "Tran"]


@pytest.mark.parametrize("said,value", REAL)
def test_a_real_name_that_contains_a_filler_word_is_read_as_a_name(said, value):
    r = p.parse_name(said)
    assert r.ok and r.value == value, (said, r)


@pytest.mark.parametrize("said", NOT_A_NAME)
def test_an_answer_or_a_filler_is_still_not_a_name(said):
    assert not p.parse_name(said).ok, said


def test_class_each_of_the_four_words_is_a_name_in_every_position_when_capitalised_and_still_a_filler_when_not():
    for word, other in itertools.product(sorted(p.NAME_AMBIGUOUS), NAME_WORDS):
        w = word.title()
        for said in (f"{w} {other}", f"{other} {w}", f"{other} {w} {NAME_WORDS[0]}", f"my name is {w} {other}"):
            r = p.parse_name(said)
            assert r.ok and w in r.value.split(), (said, r)
        for said in (f"{other} {word}", f"{word} {other}", f"{other} {word} {NAME_WORDS[1]}"):
            assert not p.parse_name(said).ok, said  # heard in lower case it is the filler it usually is, as before
            assert p.parse_name(said, named=True).ok, said  # unless the text is already a name


def test_class_every_word_of_the_stop_list_still_vetoes_a_name_wherever_it_stands():
    for word, other in itertools.product(sorted(p.NAME_STOP), NAME_WORDS[:3]):
        for said in (f"{other} {word.title()}", f"{word.title()} {other}", f"{other} {word} {NAME_WORDS[3]}"):
            assert not p.parse_name(said).ok, said
            assert not p.parse_name(said, named=True).ok, said


def test_the_four_words_are_no_longer_on_the_stop_list_and_nothing_else_moved():
    assert p.NAME_AMBIGUOUS == {"an", "to", "case", "hurt"} and not p.NAME_AMBIGUOUS & p.NAME_STOP
    assert {"yes", "no", "hello", "hi", "sorry", "please", "skip", "lawyer", "a", "i", "my", "bleeding"} <= p.NAME_STOP
    assert len(p.NAME_STOP) == 48  # the old 52 less the four


class NameLlm(Llm):
    def __init__(self, name):
        super().__init__()
        self.name = name

    async def extract_name(self, text):
        return self.name


@pytest.mark.parametrize("heard,model_name", [("uh an nguyen is my name", "An Nguyen"), ("it is um an nguyen", "an nguyen"), ("thi an tran here", "Thi An Tran"),
                                              ("I am kevin case", "kevin case"), ("the name is daniel to", "Daniel To")])
async def test_the_model_fallback_is_not_rejected_by_the_same_check(ashgrove, heard, model_name):
    ctx = make_ctx(ashgrove, llm=NameLlm(model_name))
    ctx.machine.begin()
    ctx.machine.handle_text("yes")
    assert ctx.machine.question.id == "full_name"
    assert not ctx.machine.parse(heard).ok  # the plain reader gives up on it (lower case or a stop word), so the model is asked
    got = await vp.Intake(ctx)._interpret(heard)
    assert got.ok and got.value == p.parse_name(model_name, named=True).value and got.value.lower() == model_name.lower(), got


async def test_the_model_fallback_still_refuses_what_is_not_a_name(ashgrove):
    for junk in ("yes", "I need a lawyer", "to hurt"):
        ctx = make_ctx(ashgrove, llm=NameLlm(junk))
        ctx.machine.begin()
        ctx.machine.handle_text("yes")
        assert not (await vp.Intake(ctx)._interpret("what was that")).ok, junk


def test_through_the_machine_an_nguyen_is_read_back_and_confirmed_instead_of_two_reprompts_and_a_transfer(ashgrove):
    m = IntakeMachine(ashgrove, "c1", "+14155550142", IN_HOURS)
    m.begin()
    said = []
    for u in ["yes", "My name is An Nguyen", "yes"]:
        said += [a.text for a in m.handle_text(u) if isinstance(a, Say)]
    assert "I have An Nguyen. Is that right?" in said and m.fields["full_name"].value == "An Nguyen" and m.fields["full_name"].confirmed
    assert not m.escalation
