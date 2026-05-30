import logging

logger = logging.getLogger(__name__)


def describe(html: str, url: str) -> dict:
    from crawler.parser import parse_page, parse_metadata
    meta = parse_metadata(html, url)
    parsed = parse_page(html, url)
    if parsed:
        meta["category"] = parsed["category"]
        if not meta["description"] and parsed.get("text"):
            meta["description"] = parsed["text"][:160]
    return meta
