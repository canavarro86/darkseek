"""Shared Snowball stemming for the search pipeline.

DarkSeek stems at QUERY TIME only: the FTS5 index (`pages_fts`) stores raw
title/description tokens, and every query token is expanded to
`"<stem>" OR "<original>"` before it hits MATCH (see api/search.py). Keeping the
original form in the OR means a stem that is itself a valid surface form (the
common case in English: "searching" -> "search") still matches documents that
store that form, which is why the query-time-only approach satisfies the
"searching finds search" requirement without re-indexing the corpus.

Language is detected per-token with a cheap Cyrillic test rather than langdetect:
query terms are far too short (often one word) for statistical detection to be
reliable, and the binary Latin/Cyrillic split is exactly what picks the right
Snowball algorithm here. `langdetect` stays a crawler-only dependency.

This module is the single source of truth for stemming so the parser, the
synonym expander, and the search layer never drift apart on how a token is
reduced.
"""

import re
from typing import List

import Stemmer

# Snowball stemmers for the two languages DarkSeek indexes meaningfully.
# Stemmer objects are cheap to reuse and safe for the API's request model
# (Flask's dev/gunicorn workers each get their own module import).
_STEMMERS = {
    "ru": Stemmer.Stemmer("russian"),
    "en": Stemmer.Stemmer("english"),
}

# Any Cyrillic character routes the token through the Russian stemmer.
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def detect_lang(token: str) -> str:
    """Return 'ru' for tokens containing Cyrillic, otherwise 'en'."""
    return "ru" if _CYRILLIC_RE.search(token) else "en"


def stem_word(word: str) -> str:
    """Stem a single word with the algorithm matching its script.

    Best-effort: any PyStemmer failure returns the word unchanged so search
    never breaks because of the stemmer.
    """
    try:
        return _STEMMERS[detect_lang(word)].stemWord(word)
    except Exception:
        return word


def stem_text(text: str, lang: str = "") -> str:
    """Stem every whitespace-delimited word in `text`, space-joined.

    `lang` ('en'/'ru') forces one algorithm for the whole string; any other
    value (the default) falls back to per-token script detection. Provided for
    completeness / index-time use; the live query path uses stem_query_terms.
    """
    stemmer = _STEMMERS.get(lang)
    out: List[str] = []
    for token in text.split():
        try:
            chosen = stemmer or _STEMMERS[detect_lang(token)]
            out.append(chosen.stemWord(token))
        except Exception:
            out.append(token)
    return " ".join(out)


def stem_query_terms(terms: List[str]) -> List[str]:
    """Stem each query term individually, preserving order and length."""
    return [stem_word(t) for t in terms]
