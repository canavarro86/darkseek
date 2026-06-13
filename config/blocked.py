"""Single source of truth for content blocklists (FIX 2).

Both the API (search-query refusal) and the crawler (URL/page-content refusal)
import their blocklists from here so the two can never drift apart. The lists
themselves are unchanged from their previous inline definitions in
``api/main.py`` and ``crawler/spider.py`` — only their home moved.

Matching is always a plain lowercase substring test against the relevant text;
the membership tests live with their callers, only the data lives here.
"""

from typing import FrozenSet

# Illegal-content search blocklist (CSAM + illegal goods). A search query
# containing any of these substrings is refused with an empty result set before
# it ever hits the index. Lowercase substrings; matched against the lowercased,
# sanitized query. Consumed by api/main.py:_is_blocked_query().
BLOCKED_SEARCH_TERMS: FrozenSet[str] = frozenset({
    # CSAM — original terms
    'loli', 'lolita', 'pedo', 'pedophil', 'preteen', 'pre-teen',
    'jailbait', 'child porn', 'childporn', 'cp porn', 'toddlercon',
    'underage porn', 'kids porn', 'kiddie', 'shota', 'shotacon',
    'tweenfan', 'sophie webcam',
    # CSAM — added from real query logs (zero-result CSAM queries observed in production)
    'kids peeing', 'kids pee', 'kids pic', 'kids photo',
    'children pic', 'children photo', 'children nude',
    'young michelle', 'girl pics download michelle',
    'young girl pic', 'young boy pic',
    'teen nude', 'teen naked', 'teen xxx',
    'minor nude', 'minor naked', 'minor porn',
    'baby nude', 'baby naked',
    # Illegal goods — from production query logs
    'sell human kidney', 'buy kidney', 'organ trafficking', 'buy organ',
    'buy passport', 'fake passport', 'counterfeit passport',
    'buy id card', 'fake id', 'counterfeit id',
    'hire hitman', 'kill someone', 'murder for hire',
    'buy fentanyl', 'buy heroin', 'buy cocaine', 'buy meth',
    'buy drugs online', 'dark market drugs', 'buy russian prostitute Marlin'
})

# Illegal-content crawl blocklist (CSAM). A URL whose address — or a fetched
# page whose title/description — contains any of these substrings is never
# indexed. Consumed by crawler/spider.py:_is_blocked_content().
BLOCKED_KEYWORDS: FrozenSet[str] = frozenset({
    'loli', 'lolita', 'pedo', 'pedophil', 'preteen', 'pre-teen',
    'jailbait', 'childporn', 'child porn', 'cp porn', 'toddlercon',
    'underage', 'minor porn', 'kids porn', 'kiddie', 'shota', 'shotacon',
    'sophie webcam', 'tweenfan',
})
