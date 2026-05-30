import logging

from crawler.parser import parse_metadata

logger = logging.getLogger(__name__)


def describe(html: str, url: str) -> dict:
    return parse_metadata(html, url)
