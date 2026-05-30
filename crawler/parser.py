from typing import List
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "forum":   ["forum", "board", "thread", "post", "reply", "topic", "discussion"],
    "market":  ["market", "shop", "buy", "sell", "vendor", "listing", "product", "price", "btc", "xmr"],
    "news":    ["news", "article", "blog", "press", "report", "breaking"],
    "wiki":    ["wiki", "encyclopedia", "knowledge", "guide", "howto"],
    "service": ["service", "hosting", "vpn", "email", "tool", "proxy", "api"],
}


def parse_page(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    title = _extract_title(soup)
    text = _extract_text(soup)
    links = _extract_links(soup, base_url)
    category = _guess_category(title, text)

    return {
        "title": title,
        "text": text[:5000],
        "links": links,
        "category": category,
    }


def _extract_title(soup: BeautifulSoup) -> str:
    tag = soup.find("title")
    if tag and tag.get_text(strip=True):
        return tag.get_text(strip=True)[:255]
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)[:255]
    return ""


def _extract_text(soup: BeautifulSoup) -> str:
    return " ".join(soup.get_text(separator=" ").split())


def _extract_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    seen = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc.endswith(".onion"):
            seen.add(url.split("#")[0])  # Drop fragments
    return list(seen)


def _guess_category(title: str, text: str) -> str:
    combined = (title + " " + text[:500]).lower()
    scores = {cat: sum(combined.count(kw) for kw in kws) for cat, kws in CATEGORY_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"
