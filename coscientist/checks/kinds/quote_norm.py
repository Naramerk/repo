"""Quote/source text normalization for quote_in_source (DESIGN_M1 §6.3 / K9).

Normalization version embeds ``unicodedata.unidata_version`` so replay can
detect Unicode database drift.

Documented behaviours
---------------------
* **Full Unicode casefold** — Latin/Cyrillic/Greek/… fold together. Acronyms
  like ``MET`` and ``met`` become indistinguishable after normalization.
* **Ellipsis** — U+2026 ``…`` NFKC-maps to three ASCII periods ``...``.
* **En/em dashes** — unified to ASCII ``-``, so ranges ``5–10`` and ``5-10``
  are indistinguishable after normalization.
* **Format / joiners** — U+00AD, U+200B, U+200C, U+200D, U+2060, U+FEFF
  are stripped before NFKC.
* **Superscripts/subscripts** — mapped to ``^n`` / ``_n`` markup *before*
  NFKC so ``10²`` stays ``10^2`` (not ``102``). Includes ``⁽⁾ⁿⁱ`` and ``ᵃ``.
* **Hyphenation** — ``-\\n`` is joined only between letters; both the joined
  and hyphen-kept source variants are tried when matching.
* **Token boundaries** — letter/digit word edges; digit quote edges also
  forbid adjacent ``- . , ^ _`` or digit in the source (``5 mg`` ⊄ ``-5 mg`` /
  ``0.5 mg``).
* **CJK / Thai** — scripts without whitespace tokens use a character-count
  floor (≥ ``MIN_QUOTE_CHARS_NOSPACE``) and plain substring match (no
  word-boundary check).
* **Negation / polarity** — ``quote_in_source`` checks **presence** of the
  normalized quote in the source, not the polarity of surrounding context.
  A quote of ``the result was positive`` can MET against a source that says
  ``the result was not positive``. Agents must choose quotes that carry the
  intended claim; this check does not enforce entailment.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Bump the leading digit when rules change; unidata_version tracks Unicode DB.
_NORM_RULES = "3"
NORMALIZATION_VERSION = f"{_NORM_RULES}+{unicodedata.unidata_version}"

MAX_SOURCE_BYTES = 5_000_000
MIN_QUOTE_WORD_TOKENS = 4  # meaningful letter-tokens required (space-separated scripts)
MIN_QUOTE_CHARS_NOSPACE = 8  # CJK/Thai / other scripts without whitespace tokens

_WS = re.compile(r"\s+")
# Soft line-break hyphen only between Unicode letters (not digits/spaces).
_LETTER_HYPHEN_BREAK = re.compile(
    r"(?<=[^\W\d_])-\r?\n(?=[^\W\d_])",
    re.UNICODE,
)
_LETTER_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)

# Han / Hiragana / Katakana / Thai — scripts typically written without spaces.
_NOSPACE_SCRIPT = re.compile(
    r"["
    r"\u0e00-\u0e7f"  # Thai
    r"\u3040-\u30ff"  # Hiragana + Katakana
    r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # CJK
    r"\uff66-\uff9d"  # halfwidth katakana
    r"]"
)

_DIGIT_EDGE_FORBIDDEN = frozenset("-.,^_")

_QUOTE_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2039": "'",
        "\u203a": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",  # en dash — ranges 5–10 ≡ 5-10 after unify
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)

# Inner chars for super/subscript *runs* → one ^ / _ prefix (before NFKC).
_SUPER_INNER: dict[str, str] = {
    "\u2070": "0",
    "\u00b9": "1",
    "\u00b2": "2",
    "\u00b3": "3",
    "\u2074": "4",
    "\u2075": "5",
    "\u2076": "6",
    "\u2077": "7",
    "\u2078": "8",
    "\u2079": "9",
    "\u207a": "+",
    "\u207b": "-",
    "\u207c": "=",
    "\u207d": "(",  # ⁽
    "\u207e": ")",  # ⁾
    "\u207f": "n",  # ⁿ
    "\u2071": "i",  # ⁱ
    "\u1d43": "a",  # ᵃ (modifier letter small a)
}
_SUB_INNER: dict[str, str] = {
    "\u2080": "0",
    "\u2081": "1",
    "\u2082": "2",
    "\u2083": "3",
    "\u2084": "4",
    "\u2085": "5",
    "\u2086": "6",
    "\u2087": "7",
    "\u2088": "8",
    "\u2089": "9",
    "\u208a": "+",
    "\u208b": "-",
    "\u208c": "=",
}


def _map_super_sub(text: str) -> str:
    """Map runs of super/subscripts to ``^…`` / ``_…`` so NFKC cannot flatten them."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _SUPER_INNER:
            chunk: list[str] = []
            while i < n and text[i] in _SUPER_INNER:
                chunk.append(_SUPER_INNER[text[i]])
                i += 1
            out.append("^" + "".join(chunk))
            continue
        if ch in _SUB_INNER:
            chunk = []
            while i < n and text[i] in _SUB_INNER:
                chunk.append(_SUB_INNER[text[i]])
                i += 1
            out.append("_" + "".join(chunk))
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_format_chars(text: str) -> str:
    # Soft hyphen, ZWSP, ZWNJ, ZWJ, word joiner, BOM.
    for ch in (
        "\u00ad",
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
    ):
        text = text.replace(ch, "")
    return text


def _core_normalize(text: str) -> str:
    """Shared pipeline without hyphen-break variants."""
    s = _strip_format_chars(text)
    s = _map_super_sub(s)
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_QUOTE_MAP)
    s = s.casefold()  # full Unicode casefold (MET ≡ met — documented)
    return s


def normalize_variants(text: str) -> tuple[str, str]:
    """Return (joined, hyphen_kept) normalized forms for matching.

    Letter-only ``-\\n`` soft breaks: joined removes the hyphen; kept replaces
    the break with a single ``-``. Non-letter contexts (``value -\\n5``) are
    left alone (newline becomes whitespace later).
    """
    s = _core_normalize(text)
    joined = _LETTER_HYPHEN_BREAK.sub("", s)
    kept = _LETTER_HYPHEN_BREAK.sub("-", s)
    joined = _WS.sub(" ", joined).strip()
    kept = _WS.sub(" ", kept).strip()
    return joined, kept


def normalize_for_quote_match(text: str) -> str:
    """Primary normalized form (letter hyphenation joined)."""
    return normalize_variants(text)[0]


@dataclass(frozen=True, slots=True)
class NormalizedSource:
    """Cached normalized forms for one CAS source (once per check).

    ``offset_map`` is reserved for mapping normalized offsets back to the
    raw decode; currently unused by matching (presence-only).
    """

    joined: str
    kept: str
    offset_map: tuple[int, ...] | None = None


def normalize_source_cached(source_text: str) -> NormalizedSource:
    """Normalize source once; callers must not re-normalize per quote."""
    joined, kept = normalize_variants(source_text)
    return NormalizedSource(joined=joined, kept=kept, offset_map=None)


def word_tokens(normalized: str) -> list[str]:
    """Letter-only tokens (digits/punctuation alone do not count)."""
    return _LETTER_TOKEN.findall(normalized)


def is_nospace_script_quote(normalized_quote: str) -> bool:
    """True when the quote is primarily a no-whitespace script (CJK/Thai/…)."""
    if not normalized_quote:
        return False
    if " " in normalized_quote:
        return False
    return _NOSPACE_SCRIPT.search(normalized_quote) is not None


def quote_token_ok(normalized_quote: str) -> str | None:
    """Return a refusal reason, or None if the quote meets token/char rules."""
    if not normalized_quote:
        return "quote_empty"
    tokens = word_tokens(normalized_quote)
    if len(tokens) >= MIN_QUOTE_WORD_TOKENS:
        return None
    # Scripts without spaces: character-count floor, skip word-token minimum.
    if is_nospace_script_quote(normalized_quote):
        if len(normalized_quote) >= MIN_QUOTE_CHARS_NOSPACE:
            return None
        return "quote_too_short"
    if not tokens:
        return "quote_not_meaningful"
    return "quote_too_short"


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _digit_edge_ok(needle: str, haystack: str, i: int) -> bool:
    """Digits at quote edges must not sit next to - . , ^ _ or another digit."""
    nlen = len(needle)
    if needle[0].isdigit() and i > 0:
        prev = haystack[i - 1]
        if prev.isdigit() or prev in _DIGIT_EDGE_FORBIDDEN:
            return False
    if needle[-1].isdigit():
        after_i = i + nlen
        if after_i < len(haystack):
            nxt = haystack[after_i]
            if nxt.isdigit() or nxt in _DIGIT_EDGE_FORBIDDEN:
                return False
    return True


def token_boundary_match(needle: str, haystack: str) -> bool:
    """True when ``needle`` occurs in ``haystack`` on token (+ digit-edge) boundaries.

    Prevents ``not`` ⊂ ``cannot``, ``5%`` ⊂ ``15%``, and ``5 mg`` ⊂ ``-5 mg`` /
    ``0.5 mg``.
    """
    if not needle or not haystack:
        return False
    start = 0
    nlen = len(needle)
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return False
        before_ok = i == 0 or not _is_word_char(haystack[i - 1])
        after_i = i + nlen
        after_ok = after_i >= len(haystack) or not _is_word_char(haystack[after_i])
        if before_ok and after_ok and _digit_edge_ok(needle, haystack, i):
            return True
        start = i + 1


def quote_in_normalized_source(
    norm_quote: str,
    source_text: str | NormalizedSource,
) -> bool:
    """Match quote against both hyphenation variants of the source.

    Pass a ``NormalizedSource`` when the source was already normalized (cache).
    CJK/Thai (no-space) quotes use plain substring match; others use token
    boundaries + digit-edge rules.
    """
    if isinstance(source_text, NormalizedSource):
        variants = (source_text.joined, source_text.kept)
    else:
        variants = normalize_variants(source_text)
    nospace = is_nospace_script_quote(norm_quote)
    for variant in variants:
        if nospace:
            if norm_quote and norm_quote in variant:
                return True
        elif token_boundary_match(norm_quote, variant):
            return True
    return False


def cas_text_from_bytes(raw: bytes) -> str | None:
    """Decode CAS blob as UTF-8 text; None if not text (honest refusal)."""
    if not raw:
        return ""
    if b"\x00" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
