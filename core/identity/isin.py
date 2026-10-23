"""ISO 6166 ISIN derived from a US CUSIP (#127). Not fetched.

A US ISIN is ``"US"`` + the 9-character CUSIP + one check digit. The check digit
is the Luhn digit of the letter-expanded body: A-Z map to 10-35, and every
second digit counting from the right of that expansion is doubled (a product
over 9 subtracts 9). The check digit itself, once appended, is the rightmost
digit and is not doubled.

:func:`from_cusip` returns an ISIN only for a **valid 9-character CUSIP whose
first character is a digit**. A CINS — a CUSIP-like string whose first character
is a letter, the form used for non-US securities — must not be given a ``US``
prefix: the function returns ``None``. So does a string that is not 9 characters,
or whose own CUSIP check digit does not match. A name with no such CUSIP
therefore has no ISIN (both columns stay NULL on the spine).
"""
from __future__ import annotations


def _value(ch: str) -> int | None:
    """A digit, or A-Z as 10-35. Anything else is not a CUSIP character."""
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - ord("A") + 10
    return None


def _expand(body: str) -> str:
    return "".join(str(_value(ch)) for ch in body)


def luhn_digit(body: str) -> int:
    """The ISO 6166 check digit of an unexpanded ISIN body (no check digit yet)."""
    total = 0
    for i, ch in enumerate(reversed(_expand(body))):
        n = int(ch)
        if i % 2 == 0:                 # rightmost of the body is doubled; the check digit will not be
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return (10 - (total % 10)) % 10


def luhn_ok(isin: str) -> bool:
    """Whether ``isin`` carries the ISO 6166 check digit of its own body."""
    if not isinstance(isin, str) or len(isin) < 2 or not isin[-1].isdigit():
        return False
    try:
        return luhn_digit(isin[:-1].upper()) == int(isin[-1])
    except (TypeError, ValueError):
        return False


def _cusip_ok(cusip: str) -> bool:
    """A 9-character CUSIP whose first character is a digit and whose check digit matches.

    Positions 2-8 are digits or letters. A leading letter is a CINS and fails here,
    so it never becomes a ``US`` ISIN.
    """
    if len(cusip) != 9 or not cusip[0].isdigit() or not cusip[8].isdigit():
        return False
    total = 0
    for i, ch in enumerate(cusip[:8]):
        value = _value(ch)
        if value is None:
            return False
        if i % 2 == 1:                 # CUSIP doubles the even positions, counting from the left at 1
            value *= 2
        total += value // 10 + value % 10
    return int(cusip[8]) == (10 - (total % 10)) % 10


def from_cusip(cusip: str | None) -> str | None:
    """``US`` + CUSIP + check digit, or ``None`` when ``cusip`` is not a US CUSIP.

    Surrounding space is ignored. A CINS, a wrong length, or a wrong CUSIP check
    digit returns ``None`` — never a guessed ``US`` ISIN.
    """
    if not isinstance(cusip, str):
        return None
    cusip = cusip.strip().upper()
    if not _cusip_ok(cusip):
        return None
    body = "US" + cusip
    return body + str(luhn_digit(body))
