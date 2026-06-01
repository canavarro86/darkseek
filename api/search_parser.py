"""User query -> structured FTS5 query.

Turns raw search input into a safe FTS5 MATCH expression plus the bits the rest
of the pipeline needs (terms to exclude, plain terms to highlight). Supported
syntax:

    "exact phrase"      -> FTS5 phrase, matched verbatim, NOT stemmed/expanded
    word1 word2         -> implicit AND
    word1 OR word2      -> FTS5 OR
    -word               -> exclude (handled by the caller via FTS5 NOT)
    "dark market" drugs -scam
                        -> ("dark market" AND <drugs+expansions>) excluding scam

Every emitted atom is wrapped in double quotes, so FTS5 operator characters a
user types literally (`*`, `(`, `)`, `-`, `OR`, `NEAR`) are neutralised — the
only in-string escape needed is doubling `"` per the FTS5 string-literal
grammar. This is the injection guard: a token like `a") OR pages_fts MATCH("b`
becomes the harmless phrase `"a"") OR pages_fts MATCH(""b"`.

Non-quoted single words are stemmed (api.stemmer) and, optionally, expanded with
darknet synonyms (api.synonyms) so each word atom is an OR over
{original, stem, synonyms}. Quoted phrases are left exactly as typed.

Returns a `ParsedQuery` with:
    fts_query     : positive MATCH expression (may be "" — caller must treat
                    "" as "nothing to search" and short-circuit)
    exclude_terms : bare words to exclude (caller appends them as FTS5 NOT)
    raw_terms     : plain words the user typed, for frontend highlighting
"""

import re
from dataclasses import dataclass, field
from typing import List

from .stemmer import stem_word
from .synonyms import synonyms_for

# Matches, in order: an optional leading '-', then either a "quoted phrase" or a
# run of non-space characters. Quote branch first so balanced quotes win.
_TOKEN_RE = re.compile(r'(-?)"([^"]*)"|(-?)(\S+)')


@dataclass
class ParsedQuery:
    """Structured form of a user query (see module docstring)."""

    fts_query: str = ""
    exclude_terms: List[str] = field(default_factory=list)
    raw_terms: List[str] = field(default_factory=list)


def _quote(token: str) -> str:
    """Wrap a token as an FTS5 string literal, doubling internal quotes."""
    return '"' + token.replace('"', '""') + '"'


def _has_searchable_content(token: str) -> bool:
    """True if the token has at least one letter or digit to match on.

    Strips pure punctuation (which after quoting would match nothing and only
    pollute the expression).
    """
    return any(ch.isalnum() for ch in token)


def _word_atom(word: str, expand_synonyms: bool) -> str:
    """Build the OR-group atom for one non-quoted word.

    Combines the original surface form, its stem (when different), and up to a
    few darknet synonyms, all as quoted literals OR-ed together. Keeping the
    original form in the OR is what makes query-time-only stemming work: a stem
    that is itself a valid word form still matches documents that store it.
    """
    forms: List[str] = []

    def _add_form(f: str) -> None:
        if f and _has_searchable_content(f) and f not in forms:
            forms.append(f)

    _add_form(word)
    _add_form(stem_word(word))
    if expand_synonyms:
        for syn in synonyms_for(word):
            _add_form(syn)

    if not forms:
        return ""
    if len(forms) == 1:
        return _quote(forms[0])
    return "(" + " OR ".join(_quote(f) for f in forms) + ")"


def parse_query(raw: str, expand_synonyms: bool = True) -> ParsedQuery:
    """Parse raw user input into a ParsedQuery (see module docstring)."""
    result = ParsedQuery()

    # Each emitted atom carries the connector that joins it to the previous one.
    # 'OR' only when an explicit OR operator sat between two positive atoms;
    # otherwise the default implicit AND.
    atoms: List[str] = []
    pending_or = False  # an OR operator is waiting for its right-hand atom

    for m in _TOKEN_RE.finditer(raw):
        phrase_sign, phrase, word_sign, word = m.group(1), m.group(2), m.group(3), m.group(4)

        if phrase is not None:
            sign, is_phrase, value = phrase_sign, True, phrase
        else:
            sign, is_phrase, value = word_sign, False, word

        # Bare OR (unquoted, unsigned) is the operator, not a search term.
        if not is_phrase and sign != "-" and value.upper() == "OR":
            if atoms:  # ignore a leading/dangling OR
                pending_or = True
            continue

        if sign == "-":
            # Exclusion. Phrases contribute each of their words; a bare word
            # contributes itself. These are not highlighted (they aren't hits).
            for w in (value.split() if is_phrase else [value]):
                if _has_searchable_content(w):
                    result.exclude_terms.append(w)
            continue

        # --- positive atom ---
        if is_phrase:
            phrase_clean = " ".join(value.split())
            if not _has_searchable_content(phrase_clean):
                continue
            atom = _quote(phrase_clean)
            result.raw_terms.extend(w for w in phrase_clean.split() if _has_searchable_content(w))
        else:
            atom = _word_atom(value, expand_synonyms)
            if not atom:
                continue
            result.raw_terms.append(value)

        connector = " OR " if (pending_or and atoms) else " AND "
        atoms.append((connector if atoms else "") + atom)
        pending_or = False

    result.fts_query = "".join(atoms)
    return result
