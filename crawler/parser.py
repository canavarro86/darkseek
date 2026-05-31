import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

MIN_TEXT_LENGTH = 100

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "forum":   ["forum", "board", "thread", "post", "reply", "topic", "discussion"],
    "market":  ["market", "shop", "buy", "sell", "vendor", "listing", "product", "price", "btc", "xmr"],
    "news":    ["news", "article", "blog", "press", "report", "breaking"],
    "wiki":    ["wiki", "encyclopedia", "knowledge", "guide", "howto"],
    "service": ["service", "hosting", "vpn", "email", "tool", "proxy", "api"],
}


def parse_page(html: str, base_url: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    title = _extract_title(soup)
    text = _extract_text(soup)

    if len(text) < MIN_TEXT_LENGTH:
        return None

    links = _extract_links(soup, base_url)
    category = _guess_category(title, text)
    page_type = _detect_page_type(base_url)

    return {
        "title": title,
        "text": text[:5000],
        "links": links,
        "category": category,
        "page_type": page_type,
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
    meta = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta["content"].strip() if meta and meta.get("content") else ""

    parts = [meta_desc] if meta_desc else []
    for tag in soup.find_all(["h1", "h2", "h3", "p", "li", "article"]):
        t = tag.get_text(separator=" ", strip=True)
        if t:
            parts.append(t)
    for cls in ("post", "message", "content"):
        for tag in soup.find_all("div", class_=cls):
            t = tag.get_text(separator=" ", strip=True)
            if t:
                parts.append(t)

    return " ".join(" ".join(parts).split())


_PAGINATION_RE = re.compile(r'[?&](page|start)=\d+|/page/\d+', re.IGNORECASE)


def _extract_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    seen = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc.endswith(".onion"):
            seen.add(url.split("#")[0])
        elif _PAGINATION_RE.search(href):
            full = urljoin(base_url, href)
            p = urlparse(full)
            if p.scheme in ("http", "https") and p.netloc.endswith(".onion"):
                seen.add(full.split("#")[0])
    return list(seen)


def _detect_page_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if re.search(r'/thread|/topic|/viewtopic|/showthread', path):
        return "forum_thread"
    if re.search(r'/forum|/board', path):
        return "forum_index"
    return "other"


def _guess_category(title: str, text: str) -> str:
    combined = (title + " " + text[:500]).lower()
    scores = {cat: sum(combined.count(kw) for kw in kws) for cat, kws in CATEGORY_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


def parse_metadata(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    title = ""
    tag = soup.find("title")
    if tag and tag.get_text(strip=True):
        title = tag.get_text(strip=True)[:60]
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)[:60]
    if not title:
        title = url[:60]

    description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        description = meta["content"].strip()[:160]
    if not description:
        p = soup.find("p")
        if p:
            description = p.get_text(strip=True)[:160]

    html_tag = soup.find("html")
    lang = "other"
    if html_tag and html_tag.get("lang"):
        lang = html_tag["lang"].strip()[:8] or "other"

    return {"title": title, "description": description, "category": "other", "lang": lang}
