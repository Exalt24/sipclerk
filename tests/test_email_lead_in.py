"""A filler or a lead-in before a spoken email address is not part of the address (output round 3, F2).

`Sure, it's jordan.reyes@gmail.com` was stored as surejordan.reyes@gmail.com, and the email question has no read-back, so the firm's confirmation went nowhere.
"""
import itertools

import pytest

from sipclerk.intake import parsers as p

LEADS = ["", "Sure, ", "Yeah, ", "Yes, ", "Yep, ", "Yup, ", "Um, ", "Uh, ", "Uhm, ", "Er, ", "Erm, ", "Ah, ", "Oh, ", "Hmm, ", "Well, ", "So, ", "Okay, ", "OK, ", "Alright, ",
         "Right, ", "Hi, ", "Hello, ", "Hey, ", "Actually, ", "Please, ", "My email is ", "My email address is ", "The email is ", "The email address is ",
         "Email is ", "Email address is ", "It's ", "It is ", "Its ", "That's ", "That is ", "This is ", "You can use ", "Send it to ",
         "Um... ", "Yeah. ", "Okay, um, so my email is ", "Hi, this is ", "Yeah, sure, it's ", "Well, um, it is "]
# the same address said every way a speech recogniser writes it: (what is said, what is stored)
FORMS = [
    ("jordan.reyes@gmail.com", "jordan.reyes@gmail.com"),
    ("jordan dot reyes at gmail dot com", "jordan.reyes@gmail.com"),
    ("jordan reyes at gmail dot com", "jordanreyes@gmail.com"),
    ("jordan underscore reyes at gmail dot com", "jordan_reyes@gmail.com"),
    ("jordan dash reyes at gmail dot com", "jordan-reyes@gmail.com"),
    ("jordan plus law at gmail dot com", "jordan+law@gmail.com"),
    ("j dot reyes24 at mail dot example dot org", "j.reyes24@mail.example.org"),
    ("sarah.oconnor@example.co.uk", "sarah.oconnor@example.co.uk"),
]


# CLASS-TABLE: every lead-in x every spoken form x three letter cases
@pytest.mark.parametrize("lead,form,case", list(itertools.product(LEADS, FORMS, ["lower", "title", "upper"])))
def test_a_lead_in_never_reaches_the_stored_address(lead, form, case):
    said, stored = form
    text = lead + said
    text = {"lower": text.lower(), "title": text, "upper": text.upper()}[case]
    r = p.parse_email(text)
    assert r.ok and r.value == stored, (text, r)


# An address that starts with, or holds, a word that is also a lead-in is the address: the word stays when a spoken connector follows it.
@pytest.mark.parametrize("said,stored", [
    ("sure at gmail dot com", "sure@gmail.com"),
    ("sure dot thing at gmail dot com", "sure.thing@gmail.com"),
    ("right dot smith at gmail dot com", "right.smith@gmail.com"),
    ("yes at example dot org", "yes@example.org"),
    ("hello underscore world at mail dot test", "hello_world@mail.test"),
    ("sure.thing@gmail.com", "sure.thing@gmail.com"),
    ("jordan dot sure at gmail dot com", "jordan.sure@gmail.com"),
    ("um dot reyes at gmail dot com", "um.reyes@gmail.com"),
    ("Sure, sure at gmail dot com", "sure@gmail.com"),
])
def test_a_lead_in_word_that_starts_the_address_stays(said, stored):
    assert p.parse_email(said).value == stored


@pytest.mark.parametrize("said", ["I do not have one", "no", "sure", "yeah um", "my email is", "it's"])
def test_nothing_after_the_lead_in_is_not_an_address(said):
    assert p.parse_email(said).reason == "not_an_email"
