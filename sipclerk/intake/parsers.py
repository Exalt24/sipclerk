"""Deterministic readers for each intake field type.

A speech recogniser hands back text. Whether that text is a valid name, number or date is decided
here, in plain code with tests, never by a language model. The model is only a fallback for the
fuzzy types (name, date, practice area), and its answer goes through the same validators.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from sipclerk.tenants import PracticeArea, Question


@dataclass
class Parsed:
    ok: bool
    value: object = None
    reason: str = ""  # machine-readable: too_short, need_last_name, not_a_date, ambiguous, ...
    remainder: str = ""  # text left after a "no, it is ..." correction
    skipped: bool = False
    detail: str = ""


FAIL = Parsed(False, reason="unparsed")

DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9"}
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "couple": 2, "few": 3, "several": 4}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SKIP_WORDS = ("skip", "pass", "no thanks", "no thank you", "rather not", "i would rather not", "don't have", "do not have",
              "prefer not", "not right now", "none")


def _squash(text: str, keep: str, digits: bool = True) -> str:
    """Every character that is not a letter (or a digit, unless `digits` is off) of any language, a combining mark, or one of `keep` becomes a space."""
    return "".join(c if ((c.isalnum() and (digits or not c.isdigit())) or c in keep or unicodedata.category(c).startswith("M")) else " " for c in text)


def _norm(text: str) -> str:
    """Lower case, NFC, letters of any language kept (a name said as `José García` must stay that), everything else a space."""
    t = unicodedata.normalize("NFC", text).lower().replace("’", "'")
    t = _squash(t, "'@.-")
    return re.sub(r"\s+", " ", t).strip()


def _words(text: str) -> list[str]:
    return [w.strip(".-") for w in _norm(text).split() if w.strip(".-")]


# ------------------------------------------------------------------ yes / no

YES = {"yes", "yeah", "yep", "yup", "sure", "correct", "right", "absolutely", "affirmative", "okay", "ok", "mhm", "uh-huh",
       "definitely", "exactly", "indeed"}
NO = {"no", "nope", "nah", "wrong", "incorrect", "negative", "not"}


def parse_yesno(text: str) -> Parsed:
    words = _words(text)
    if not words:
        return Parsed(False, reason="empty")
    head = words[:5]
    yes_i = next((i for i, w in enumerate(head) if w in YES or w == "yes"), None)
    no_i = next((i for i, w in enumerate(head) if w in NO or w in ("isn't", "wasn't", "don't", "didn't")), None)
    if "that's" in head and "right" in head and no_i is None:
        yes_i = head.index("right")
    if yes_i is not None and no_i is not None:
        return Parsed(False, reason="ambiguous")
    if yes_i is not None:
        return Parsed(True, True, remainder=" ".join(words[yes_i + 1:]))
    if no_i is not None:
        rest = words[no_i + 1:]
        while rest and rest[0] in ("it's", "its", "it", "is", "that's", "actually", "sorry", "not", "the", "name", "number"):
            rest = rest[1:]
        return Parsed(True, False, remainder=" ".join(rest))
    return Parsed(False, reason="unclear")


# ------------------------------------------------------------------ name

NAME_LEAD = re.compile(r"^(?:(?:uh|um|well|yes|yeah|hi|hello|hey|sure|okay|ok)[,\s]+)*"
                       r"(?:my (?:full )?name(?: is|'s)|this is|it is|it's|its|i am|i'm|name is|name's|call me|they call me)\s+", re.I)
NAME_STOP = {"yes", "no", "the", "and", "but", "not", "what", "why", "how", "please", "sorry", "hello", "hi", "um", "uh",
             "skip", "lawyer", "attorney", "client", "person", "someone", "phone", "number", "accident", "injured",
             "is", "was", "are", "were", "my", "i", "need", "want", "help", "have", "has", "had", "with", "for", "at",
             "a", "it", "that", "this", "me", "we", "you", "bleeding"}
# Words that are an answer or a filler AND a real part of a name (An Nguyen, Thi An Tran, Daniel To, Kevin Case, Mary Hurt). Heard capitalised next to a word that
# is only a name, they are a name; said on their own, or in lower case, they are still the filler they usually are.
NAME_AMBIGUOUS = {"an", "to", "case", "hurt"}


def parse_name(text: str, *, named: bool = False) -> Parsed:
    """`named` is for text that is already a name (what the model extracted): the capital letter is then not needed to read An, To, Case or Hurt as part of it."""
    raw = _squash(unicodedata.normalize("NFC", text).replace("’", "'"), "'-", digits=False)
    raw = NAME_LEAD.sub("", raw.strip())
    tokens = [t.strip("-'") for t in raw.split() if t.strip("-'")]
    if not tokens:
        return Parsed(False, reason="empty")
    if len(tokens) > 5 or any(t.lower() in NAME_STOP for t in tokens):
        return Parsed(False, reason="not_a_name")
    ambiguous = [t for t in tokens if t.lower() in NAME_AMBIGUOUS]
    if ambiguous and (len(ambiguous) == len(tokens) or not (named or all(t[:1].isupper() for t in ambiguous))):
        return Parsed(False, reason="not_a_name")
    if re.search(r"\d", text) and not re.search(r"[^\W\d_]{2}", text):
        return Parsed(False, reason="not_a_name")
    name = " ".join(t[:1].upper() + t[1:].lower() if not re.match(r"^(mc|o')", t.lower()) else t[:1].upper() + t[1:2] + t[2:3].upper() + t[3:].lower()
                    for t in tokens)
    name = unicodedata.normalize("NFC", name)
    if len(tokens) < 2:
        return Parsed(False, name, reason="need_last_name", detail=name)
    return Parsed(True, name)


# ------------------------------------------------------------------ phone


def spoken_digits(text: str) -> str:
    """Digits from speech: 'four one five double five oh one two' and '415-555-0123' both work."""
    out: list[str] = []
    words = [w.strip(".,") for w in _norm(text).replace("-", " ").split()]
    words = [w for w in words if w]
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("double", "triple") and i + 1 < len(words):
            nxt = words[i + 1]
            d = DIGIT_WORDS.get(nxt) or (nxt if nxt.isdigit() and len(nxt) == 1 else None)
            if d:
                out.append(d * (2 if w == "double" else 3))
                i += 2
                continue
        if w in DIGIT_WORDS and not (w == "o" and len(words) > 1 and not any(x in DIGIT_WORDS or x.isdigit() for x in words)):
            out.append(DIGIT_WORDS[w])
        elif re.fullmatch(r"[0-9]+", w):
            out.append(w)
        i += 1
    return "".join(out)


def parse_phone(text: str) -> Parsed:
    digits = spoken_digits(text)
    return validate_phone_digits(digits)


def validate_phone_digits(digits: str) -> Parsed:
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 10:
        return Parsed(False, digits, reason="too_short", detail=str(len(digits)))  # value: the digits heard, so a caller can add the rest
    if len(digits) > 10:
        return Parsed(False, digits, reason="too_long", detail=str(len(digits)))
    if digits[0] in "01" or digits[3] in "01":
        return Parsed(False, reason="not_a_phone_number", detail="area or exchange code cannot start with 0 or 1")
    return Parsed(True, "+1" + digits)


# ------------------------------------------------------------------ email

EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$")


def is_skip(text: str) -> bool:
    n = _norm(text)
    return any(n == s or n.startswith(s + " ") or s in n.split(". ")[0] for s in SKIP_WORDS)


# What a caller says before the address: a filler (`um`, `yeah`, `sure`) or a lead-in (`my email is`, `it's`). A word that a spoken connector follows is the
# start of the address (`sure at gmail dot com`), so it stays.
EMAIL_LEAD = re.compile(
    r"^(?:(?:uh|um|uhm|er|erm|ah|oh|hmm|well|so|okay|ok|alright|yeah|yes|yep|yup|sure|right|hi|hello|hey|actually|please|"
    r"my email(?: address)?(?: is)?|the email(?: address)?(?: is)?|email(?: address)? is|it's|it is|its|that's|that is|this is|"
    r"you can use|send it to)\.*(?=\s)(?!\s+(?:dot|at|underscore|dash|plus)\b)\s*)+")


def parse_email(text: str) -> Parsed:
    n = EMAIL_LEAD.sub("", _norm(text)).strip()
    n = n.replace(" at ", "@").replace(" dot ", ".").replace(" underscore ", "_").replace(" dash ", "-").replace(" plus ", "+")
    n = re.sub(r"\s+", "", n).strip(".")
    if EMAIL_RE.match(n):
        return Parsed(True, n)
    return Parsed(False, reason="not_an_email")


# ------------------------------------------------------------------ date


def _num(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m])
    return date(y, m + 1, day)


ORDINAL_DAYS = {w: i + 1 for i, w in enumerate(["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
                                                "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth", "twentieth"])}
ORDINAL_DAYS.update({f"twenty {w}": 20 + i for i, w in enumerate(["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"], 1)})
ORDINAL_DAYS.update({"thirtieth": 30, "thirty first": 31})
_ORD = "|".join(ORDINAL_DAYS)
_MONTH = "|".join(MONTHS)
# Words that, in front of "may", say it is the month (`in may`, `since may`, `last may`).
_MAY_AFTER_PREPOSITION = {"in", "of", "on", "since", "during", "early", "late", "mid", "until", "till", "by", "from", "last", "this", "around", "about",
                          "before", "after", "between", "through", "to"}

# Words that may follow the month (`May of last year`, `May last year`, `May two years ago`) and never follow the verb, which takes a verb or an adverb.
_MAY_BEFORE_DATE_WORD = {"of", "or", "and", "last", "this", "the"} | set(NUMBER_WORDS)


def _named_month(n: str) -> int | None:
    """The month the caller named, or None. `may` is a month only in a date position: after a preposition, before a day or year, or as the last word;
    in `it may have been in August` it is the verb, and August is the month."""
    words = n.split()
    for i, w in enumerate(words):
        name = w.strip(".-")
        if name not in MONTHS:
            continue
        if name == "may":
            before = words[i - 1] if i else ""
            after = words[i + 1] if i + 1 < len(words) else ""
            if not (before in _MAY_AFTER_PREPOSITION or not after or after.isdigit() or after in _MAY_BEFORE_DATE_WORD):
                continue
        return MONTHS.index(name) + 1
    return None


def _ordinal_days(n: str) -> str:
    """`the third of March`, `May fifth` and `the fifth of last month` as `3 march`, `may 5`, `5 last month`: a day said in words beside a month."""
    n = re.sub(r"\b(twenty|thirty)-", r"\1 ", n)
    n = re.sub(rf"\b({_ORD})\b(?= (?:of )?(?:{_MONTH}|last month|this month)\b)", lambda m: str(ORDINAL_DAYS[m.group(1)]), n)
    return re.sub(rf"\b({_MONTH}) ({_ORD})\b", lambda m: f"{m.group(1)} {ORDINAL_DAYS[m.group(2)]}", n)


def _years_back(n: str) -> int | None:
    """How many years back a relative phrase says (`last year` 1, `two years ago` 2, `this year` 0), or None."""
    if re.search(r"\blast year\b", n):
        return 1
    if re.search(r"\bthis year\b", n):
        return 0
    if m := re.search(r"\b(\w+)(?: of)? years? ago\b", n):
        return _num(m.group(1))
    return None


def _day_beside(n: str, month: int) -> int:
    """The day number the caller gave with the month (`3 march`, `march 3`), else the first number said; 1 when there is none."""
    name = MONTHS[month - 1]
    rest = re.sub(r"\b(?:19|20)\d\d\b", " ", n)
    rest = re.sub(r"\b\w+(?: of)? (?:days?|weeks?|months?|years?) ago\b", " ", rest)  # the number in `two years ago` is not a day
    m = re.search(rf"\b([0-3]?\d)\b(?: of)? {name}\b", rest) or re.search(rf"\b{name} ([0-3]?\d)\b", rest) or re.search(r"\b([0-3]?\d)\b", rest)
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 31 else 1


def parse_date(text: str, today: date, max_age_days: int | None = None) -> Parsed:
    n = _ordinal_days(_norm(text))
    n = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", n)
    found: date | None = None
    month = _named_month(n)
    if re.search(r"\b(?:tomorrow|next (?:week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", n):
        return Parsed(False, reason="in_the_future", detail="a date that has not happened yet")
    if re.search(r"\byesterday\b", n):
        found = today - timedelta(days=1)
    elif re.search(r"\btoday\b|\bthis morning\b|\btonight\b", n):
        found = today
    elif month:  # a month the caller named wins over a relative word: `March of last year` is March, and `last year` only says which year
        year_m = re.search(r"\b(19\d\d|20\d\d)\b", n)
        back = _years_back(n)
        if year_m:
            year = int(year_m.group(1))
        elif back is not None:
            year = today.year - back
        else:  # no year said: the most recent such month that is not in the future
            year = today.year if date(today.year, month, 1) <= today else today.year - 1
        try:
            found = date(year, month, _day_beside(n, month))
        except ValueError:
            return Parsed(False, reason="not_a_date")
    elif m := re.search(r"\b([0-3]?\d)(?: of)? (last|this) month\b", n):  # `the 5th of last month`
        base = _add_months(today, 1) if m.group(2) == "last" else today
        try:
            found = date(base.year, base.month, int(m.group(1)))
        except ValueError:
            return Parsed(False, reason="not_a_date")
    elif m := re.search(r"\b(\w+)(?: of)? (day|days|week|weeks|month|months|year|years) ago\b", n):
        k = _num(m.group(1))
        if k is not None:
            unit = m.group(2).rstrip("s")
            found = {"day": today - timedelta(days=k), "week": today - timedelta(weeks=k),
                     "month": _add_months(today, k), "year": _add_months(today, 12 * k)}[unit]
    elif m := re.search(r"\blast (week|month|year)\b", n):
        found = {"week": today - timedelta(weeks=1), "month": _add_months(today, 1), "year": _add_months(today, 12)}[m.group(1)]
    elif m := re.search(r"\blast (" + "|".join(WEEKDAYS) + r")\b", n):
        back = (today.weekday() - WEEKDAYS.index(m.group(1))) % 7 or 7
        found = today - timedelta(days=back)
    elif year_m := re.search(r"\b(19\d\d|20\d\d)\b", n):
        found = date(int(year_m.group(1)), 6, 30)
    if found is None:
        return Parsed(False, reason="not_a_date")
    if found > today:
        return Parsed(False, reason="in_the_future", detail=found.isoformat())
    if max_age_days is not None and (today - found).days > max_age_days:
        return Parsed(False, reason="too_old", detail=found.isoformat())
    return Parsed(True, found.isoformat())


# ------------------------------------------------------------------ enum and free text


def _hits(text: str, keywords: list[str]) -> int:
    n = " " + _norm(text) + " "
    return sum(1 for k in keywords if re.search(rf"(?<![a-z]){re.escape(k.lower())}(?![a-z])", n))


def parse_area(text: str, areas: list[PracticeArea]) -> Parsed:
    scored = sorted(((_hits(text, a.keywords), a.id) for a in areas), reverse=True)
    if not scored or scored[0][0] == 0:
        return Parsed(False, reason="no_match")
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return Parsed(False, reason="ambiguous", detail=",".join(i for s, i in scored if s == scored[0][0]))
    return Parsed(True, scored[0][1])


def parse_option(text: str, options: list[str]) -> Parsed:
    n = _norm(text)
    for o in options:
        if re.search(rf"(?<![a-z]){re.escape(o.lower())}(?![a-z])", n):
            return Parsed(True, o)
    return Parsed(False, reason="no_match")


FILLER_LEAD = re.compile(r"^(?:(?:uh|um|so|well|okay|ok|yeah|yes|hi)[,\s]+)+", re.I)


def parse_text(text: str, min_words: int) -> Parsed:
    t = FILLER_LEAD.sub("", text.strip())
    t = re.sub(r"\s+", " ", t)[:600]
    words = [w for w in re.split(r"\s+", t) if re.search(r"[^\W_]", w)]
    if len(words) < min_words:
        return Parsed(False, reason="too_short", detail=str(len(words)))
    return Parsed(True, t)


# ------------------------------------------------------------------ dispatcher


def parse_for(question: Question, text: str, *, today: date, areas: list[PracticeArea]) -> Parsed:
    t = question.type
    if not question.required and is_skip(text):
        return Parsed(True, None, skipped=True)
    if t == "name":
        return parse_name(text)
    if t == "phone":
        return parse_phone(text)
    if t == "email":
        return parse_email(text)
    if t == "date":
        return parse_date(text, today, question.max_age_days)
    if t == "yesno":
        return parse_yesno(text)
    if t == "enum":
        return parse_area(text, areas) if question.id == "practice_area" else parse_option(text, question.options)
    return parse_text(text, question.min_words)


def speak_digits(e164: str) -> str:
    """'+14155550123' as 'four one five, five five five, zero one two three' for a read-back."""
    d = e164[-10:]
    names = {v: k for k, v in DIGIT_WORDS.items() if k not in ("oh", "o")}
    grp = [d[:3], d[3:6], d[6:]]
    return ", ".join(" ".join(names[c] for c in g) for g in grp)


def speak_partial(digits: str) -> str:
    """Digits heard so far, in threes, for 'I have four one five so far'."""
    names = {v: k for k, v in DIGIT_WORDS.items() if k not in ("oh", "o")}
    return ", ".join(" ".join(names[c] for c in digits[i:i + 3]) for i in range(0, len(digits), 3))


def readback(question: Question, value: object) -> str:
    if question.type == "phone":
        return speak_digits(str(value))
    if question.type == "date":
        y, m, d = str(value).split("-")
        return f"{MONTHS[int(m) - 1].capitalize()} {int(d)}, {y}"
    if question.type == "email":
        return str(value).replace("@", " at ").replace(".", " dot ")
    return str(value)


def mask_for_transcript(text: str) -> str:
    """Hide digit runs, emails and spoken digit strings before text leaves for another system."""
    t = re.sub(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+", "[email]", text)
    t = re.sub(r"\+?\d[\d\-\s().]{6,}\d", "[number]", t)
    spoken = r"(?:(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)[\s,.\-]+){6,}(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
    return re.sub(spoken, "[number]", t, flags=re.I)
