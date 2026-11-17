"""web_search (#11) and web_extract (#12).

Search backends (auto-selected by available keys): Tavily, Brave, Exa,
Firecrawl, SearXNG, then a keyless DuckDuckGo HTML fallback.
"""

from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
import urllib.parse
from html.parser import HTMLParser

from hermes.providers import http
from hermes.tools.registry import ToolContext, ToolError, tool


class _TextExtractor(HTMLParser):
    """Minimal HTML -> markdown-ish text converter."""

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "iframe"}
    BLOCK = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table"}

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self.base_url = base_url

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "#")):
                self._href = urllib.parse.urljoin(self.base_url, href)
                self._link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href:
            text = " ".join("".join(self._link_text).split())
            if text:
                self.links.append((text, self._href))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return "\n".join(line.strip() for line in raw.splitlines()).strip()


def html_to_text(page: str, base_url: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    parser = _TextExtractor(base_url)
    try:
        parser.feed(page)
    except Exception:
        pass
    return parser.title.strip(), parser.text(), parser.links


def is_private_url(url: str) -> bool:
    """SSRF guard: block localhost / private network targets (override: HERMES_ALLOW_PRIVATE_URLS=1)."""
    if os.environ.get("HERMES_ALLOW_PRIVATE_URLS"):
        return False
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except (socket.gaierror, ValueError):
        return False
    return False


# ---------------------------------------------------------------- search backends

def _tavily(q: str, n: int) -> list[dict]:
    data = http.post_json("https://api.tavily.com/search", {"api_key": os.environ["TAVILY_API_KEY"], "query": q, "max_results": n})
    return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content", "")[:500]} for r in data.get("results", [])]


def _brave(q: str, n: int) -> list[dict]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "count": n})
    data = http.get_json(url, {"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"})
    return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description", "")} for r in data.get("web", {}).get("results", [])]


def _exa(q: str, n: int) -> list[dict]:
    data = http.post_json("https://api.exa.ai/search", {"query": q, "numResults": n, "contents": {"text": {"maxCharacters": 500}}},
                          {"x-api-key": os.environ["EXA_API_KEY"]})
    return [{"title": r.get("title"), "url": r.get("url"), "snippet": (r.get("text") or "")[:500]} for r in data.get("results", [])]


def _firecrawl(q: str, n: int) -> list[dict]:
    data = http.post_json("https://api.firecrawl.dev/v1/search", {"query": q, "limit": n},
                          {"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"})
    return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description", "")} for r in data.get("data", [])]


def _searxng(q: str, n: int) -> list[dict]:
    base = os.environ["SEARXNG_URL"].rstrip("/")
    data = http.get_json(f"{base}/search?" + urllib.parse.urlencode({"q": q, "format": "json"}))
    return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content", "")} for r in data.get("results", [])[:n]]


def _duckduckgo(q: str, n: int) -> list[dict]:
    _, page = http.get_text("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q}))
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)', page, re.S):
        href = html.unescape(m.group(1))
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        title = re.sub(r"<[^>]+>", "", html.unescape(m.group(2))).strip()
        snip_m = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', m.group(3), re.S)
        snippet = re.sub(r"<[^>]+>", "", html.unescape(snip_m.group(1))).strip() if snip_m else ""
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            break
    return results


SEARCH_BACKENDS = [
    ("tavily", "TAVILY_API_KEY", _tavily),
    ("brave", "BRAVE_API_KEY", _brave),
    ("exa", "EXA_API_KEY", _exa),
    ("firecrawl", "FIRECRAWL_API_KEY", _firecrawl),
    ("searxng", "SEARXNG_URL", _searxng),
    ("duckduckgo", "", _duckduckgo),
]


def search(query: str, n: int = 5, backend: str = "auto") -> tuple[str, list[dict]]:
    errors = []
    for name, env, fn in SEARCH_BACKENDS:
        if backend not in ("auto", name):
            continue
        if env and not os.environ.get(env):
            continue
        try:
            return name, fn(query, n)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise ToolError("all search backends failed: " + "; ".join(errors))


@tool(
    "web_search",
    "web",
    "Search the web. Returns titles, URLs and snippets. Follow up with web_extract to read pages.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
)
def web_search(args: dict, ctx: ToolContext):
    backend = (ctx.config.get("web", {}) or {}).get("backend", "auto")
    name, results = search(args["query"], min(int(args.get("max_results") or 5), 20), backend)
    return {"backend": name, "query": args["query"], "results": results}


def fetch_page(url: str, max_chars: int = 15000) -> dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if is_private_url(url):
        return {"url": url, "error": "refusing to fetch a private/internal address"}
    if os.environ.get("FIRECRAWL_API_KEY"):
        try:
            data = http.post_json("https://api.firecrawl.dev/v1/scrape", {"url": url, "formats": ["markdown"]},
                                  {"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"})
            md = (data.get("data") or {}).get("markdown", "")
            if md:
                return {"url": url, "title": ((data.get("data") or {}).get("metadata") or {}).get("title", ""), "content": md[:max_chars], "truncated": len(md) > max_chars}
        except Exception:
            pass
    try:
        ctype, body = http.get_text(url, {"Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"})
    except http.HTTPError as exc:
        return {"url": url, "error": f"HTTP {exc.status}"}
    except OSError as exc:
        return {"url": url, "error": str(exc)}
    if "html" in ctype or body.lstrip().startswith("<"):
        title, text, _ = html_to_text(body, url)
    else:
        title, text = "", body
    return {"url": url, "title": title, "content": text[:max_chars], "truncated": len(text) > max_chars, "chars": len(text)}


@tool(
    "web_extract",
    "web",
    "Fetch one or more URLs and return their readable text content (markdown-ish).",
    {
        "type": "object",
        "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "description": "Up to 5 URLs"},
            "max_chars": {"type": "integer", "description": "Per-page character cap"},
        },
        "required": ["urls"],
    },
)
def web_extract(args: dict, ctx: ToolContext):
    cap = int(args.get("max_chars") or (ctx.config.get("web", {}) or {}).get("max_extract_chars", 15000))
    urls = args["urls"][:5] if isinstance(args["urls"], list) else [str(args["urls"])]
    return {"pages": [fetch_page(u, cap) for u in urls]}
