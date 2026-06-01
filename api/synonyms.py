"""Curated darknet synonym dictionary for query expansion.

A search for `btc` should also surface pages that only say "bitcoin"; a search
for `market` should reach "shop", "vendor", "магазин". These relationships are
domain-specific and bilingual (English + Russian), so they're hand-curated here
rather than learned.

`expand_query_terms` is deliberately conservative: it only expands NON-quoted
single terms, caps expansions per term, and never expands an operator. Over-
expansion destroys precision (every market page matching every shop query), so
the cap matters as much as the dictionary.

Matching is done on the lowercased term and, as a fallback, on its stem, so
"markets" still finds the "market" group.
"""

from typing import Dict, List

from .stemmer import stem_word

# Each inner list is one synonym group: any member expands to the others.
# Groups mix English + Russian because the corpus is bilingual. Keep members
# lowercase and single-token (multi-word entries can't match a single term).
SYNONYM_GROUPS: List[List[str]] = [
    # --- commerce -----------------------------------------------------------
    ["market", "marketplace", "shop", "store", "vendor", "bazaar", "магазин", "рынок", "торговля"],
    ["exchange", "swap", "mixer", "tumbler", "обменник", "обмен", "конвертер"],
    ["escrow", "deposit", "guarantor", "гарант", "депозит"],
    ["price", "cost", "rate", "цена", "стоимость", "прайс"],
    ["order", "checkout", "cart", "заказ", "корзина"],
    # --- community ----------------------------------------------------------
    ["forum", "board", "community", "discussion", "imageboard", "форум", "доска", "обсуждение"],
    ["chat", "messenger", "im", "jabber", "чат", "мессенджер"],
    ["review", "feedback", "rating", "отзыв", "рейтинг"],
    # --- privacy / security -------------------------------------------------
    ["vpn", "proxy", "tunnel", "впн", "прокси"],
    ["anonymity", "anonymous", "privacy", "анонимность", "приватность"],
    ["security", "infosec", "opsec", "безопасность", "защита"],
    ["encryption", "encrypted", "pgp", "gpg", "шифрование", "шифр"],
    ["tor", "onion", "darknet", "darkweb", "тор", "даркнет"],
    # --- documents / identity ----------------------------------------------
    ["docs", "documents", "id", "identity", "документы", "документ"],
    ["passport", "passports", "паспорт"],
    ["fake", "forged", "counterfeit", "novelty", "фейк", "поддельный", "подделка"],
    ["license", "licence", "permit", "права", "лицензия"],
    # --- crypto -------------------------------------------------------------
    ["bitcoin", "btc", "биткоин", "биток"],
    ["monero", "xmr", "монеро"],
    ["ethereum", "eth", "эфир", "эфириум"],
    ["crypto", "cryptocurrency", "coin", "криптовалюта", "крипта", "монета"],
    ["wallet", "кошелек", "кошелёк"],
    # --- hosting / infra ----------------------------------------------------
    ["hosting", "host", "server", "vps", "bulletproof", "хостинг", "сервер"],
    ["domain", "dns", "registrar", "домен"],
    ["email", "mail", "mailbox", "почта", "мейл"],
    ["database", "db", "dump", "leak", "база", "дамп", "слив", "утечка"],
    # --- hacking ------------------------------------------------------------
    ["hack", "hacking", "exploit", "breach", "взлом", "эксплойт"],
    ["malware", "virus", "trojan", "ransomware", "rat", "вирус", "троян"],
    ["vulnerability", "vuln", "cve", "0day", "zeroday", "уязвимость"],
    ["botnet", "ddos", "stresser", "ботнет"],
    ["phishing", "scampage", "фишинг"],
    # --- financial fraud ----------------------------------------------------
    ["card", "cards", "cc", "cvv", "dumps", "карты", "карта", "кардинг"],
    ["bank", "banking", "account", "банк", "счет", "счёт"],
    ["paypal", "pp", "transfer", "перевод"],
    ["money", "cash", "funds", "деньги", "нал", "обнал"],
    ["counterfeit", "fakemoney", "fakenotes", "фальшивые", "фальшак"],
    # --- substances ---------------------------------------------------------
    ["drugs", "drug", "narcotics", "наркотики", "вещества", "клад"],
    ["weed", "cannabis", "marijuana", "ganja", "шишки", "гашиш"],
    ["pills", "tabs", "таблетки", "колеса"],
    # --- weapons ------------------------------------------------------------
    ["weapon", "weapons", "gun", "guns", "firearm", "оружие", "ствол"],
    ["ammo", "ammunition", "патроны"],
    # --- services -----------------------------------------------------------
    ["service", "services", "tool", "tools", "сервис", "услуга", "инструмент"],
    ["guide", "tutorial", "howto", "manual", "гайд", "мануал", "инструкция"],
    ["news", "press", "article", "blog", "новости", "статья", "блог"],
    ["wiki", "encyclopedia", "directory", "catalog", "вики", "каталог"],
    ["search", "engine", "index", "поиск", "поисковик"],
    ["software", "app", "program", "warez", "софт", "программа"],
    ["gift", "giftcard", "voucher", "подарочная", "сертификат"],
    ["job", "work", "vacancy", "работа", "вакансия"],
    ["hire", "hitman", "killer", "наемник", "киллер"],
    ["bet", "betting", "casino", "gambling", "ставки", "казино"],
]

# Cap on synonyms added per query term. Past ~3 the query loses precision: a
# four-synonym OR makes almost every market page match almost every shop query.
MAX_SYNONYMS_PER_TERM = 3


def _build_index() -> Dict[str, List[str]]:
    """Map every member term -> the other members of its group.

    A term appearing in multiple groups gets its lists concatenated (de-duped,
    order preserved), so e.g. "counterfeit" links both fraud and fake-docs.
    """
    index: Dict[str, List[str]] = {}
    for group in SYNONYM_GROUPS:
        for term in group:
            bucket = index.setdefault(term, [])
            for other in group:
                if other != term and other not in bucket:
                    bucket.append(other)
    return index


_SYNONYM_INDEX = _build_index()
# Stem -> first matching group members, so inflected inputs ("markets") resolve.
_STEM_INDEX: Dict[str, List[str]] = {}
for _term, _syns in _SYNONYM_INDEX.items():
    _STEM_INDEX.setdefault(stem_word(_term), _syns)


def synonyms_for(term: str) -> List[str]:
    """Return up to MAX_SYNONYMS_PER_TERM synonyms for a single term.

    Tries an exact (lowercased) match first, then a stemmed match. Returns an
    empty list when the term isn't in the dictionary.
    """
    key = term.lower()
    syns = _SYNONYM_INDEX.get(key)
    if syns is None:
        syns = _STEM_INDEX.get(stem_word(key), [])
    return syns[:MAX_SYNONYMS_PER_TERM]


def expand_query_terms(terms: List[str]) -> List[str]:
    """Expand a list of bare query terms with their darknet synonyms.

    Returns the originals followed by any synonyms (de-duplicated, order
    preserved). Callers pass ONLY non-quoted terms here — phrases must not be
    expanded. Terms with no dictionary entry pass through unchanged.
    """
    out: List[str] = []
    seen = set()

    def _add(t: str) -> None:
        low = t.lower()
        if low not in seen:
            seen.add(low)
            out.append(t)

    for term in terms:
        _add(term)
        for syn in synonyms_for(term):
            _add(syn)
    return out
