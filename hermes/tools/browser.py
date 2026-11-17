"""Browser automation (#17, #61 browser_exec, #62 navigation, #63 page snapshots,
#64 browser vision, #65 form filling).

Two engines:
* ``playwright`` - real Chromium (``pip install playwright && playwright install chromium``)
* ``lite``       - dependency-free HTTP browser (links + HTML forms, no JavaScript)

Snapshots list interactive elements with refs (``@e3``) that the click/type
tools accept.
"""

from __future__ import annotations

import base64
import queue
import threading
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from hermes.providers import http
from hermes.tools.registry import ToolContext, ToolError, tool
from hermes.tools.web import html_to_text, is_private_url

# ---------------------------------------------------------------- lite engine


class _FormParser(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.elements: list[dict] = []
        self.forms: list[dict] = []
        self._form: dict | None = None
        self._capture: dict | None = None

    def _add(self, el: dict) -> None:
        el["ref"] = f"e{len(self.elements) + 1}"
        if self._form is not None:
            el["form"] = len(self.forms) - 1
            self._form["fields"].append(el)
        self.elements.append(el)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = {"action": urllib.parse.urljoin(self.base, a.get("action") or self.base),
                          "method": (a.get("method") or "get").lower(), "fields": []}
            self.forms.append(self._form)
        elif tag == "a" and a.get("href") and not a["href"].startswith(("javascript:", "#", "mailto:")):
            self._capture = {"kind": "link", "href": urllib.parse.urljoin(self.base, a["href"]), "text": ""}
            self._add(self._capture)
        elif tag == "input":
            itype = (a.get("type") or "text").lower()
            if itype == "hidden":
                if self._form is not None:
                    self._form["fields"].append({"kind": "hidden", "name": a.get("name"), "value": a.get("value", "")})
                return
            kind = "submit" if itype in ("submit", "image") else "input"
            self._add({"kind": kind, "type": itype, "name": a.get("name"), "value": a.get("value", ""),
                       "text": a.get("placeholder") or a.get("aria-label") or a.get("name") or itype,
                       "checked": "checked" in a})
        elif tag == "textarea":
            self._capture = {"kind": "input", "type": "textarea", "name": a.get("name"), "value": "", "text": a.get("placeholder") or a.get("name") or "textarea"}
            self._add(self._capture)
        elif tag == "select":
            self._capture = {"kind": "select", "name": a.get("name"), "options": [], "value": "", "text": a.get("name") or "select"}
            self._add(self._capture)
        elif tag == "option" and self.elements and self.elements[-1]["kind"] == "select":
            sel = self.elements[-1]
            sel["options"].append(a.get("value", ""))
            if not sel["value"] or "selected" in a:
                sel["value"] = a.get("value", "")
        elif tag == "button":
            btype = (a.get("type") or "submit").lower()
            self._capture = {"kind": "submit" if btype == "submit" else "button", "name": a.get("name"), "value": a.get("value", ""), "text": ""}
            self._add(self._capture)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        if tag in ("a", "button", "textarea", "select"):
            self._capture = None

    def handle_data(self, data):
        if self._capture is not None and self._capture["kind"] in ("link", "button", "submit"):
            self._capture["text"] = (self._capture["text"] + " " + data.strip()).strip()[:120]


class LiteBrowser:
    def __init__(self):
        self.url = ""
        self.html = ""
        self.history: list[str] = []
        self.elements: list[dict] = []
        self.forms: list[dict] = []

    def _load(self, url: str, data: bytes | None = None, method: str = "GET") -> None:
        if is_private_url(url):
            raise ToolError("refusing to browse a private/internal address")
        headers = {"Accept": "text/html,*/*"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        _, _, body = http.request(method, url, headers=headers, data=data, timeout=30)
        if self.url:
            self.history.append(self.url)
        self.url, self.html = url, body.decode("utf-8", "replace")
        p = _FormParser(url)
        try:
            p.feed(self.html)
        except Exception:
            pass
        self.elements, self.forms = p.elements, p.forms

    def navigate(self, url: str) -> dict:
        self._load(url)
        return self.snapshot()

    def snapshot(self, full: bool = False) -> dict:
        title, text, _ = html_to_text(self.html, self.url)
        els = []
        for e in self.elements[:200]:
            label = e.get("text") or e.get("name") or ""
            extra = f" = {e['value']!r}" if e["kind"] in ("input", "select") and e.get("value") else ""
            els.append(f"@{e['ref']} [{e['kind']}{':' + e['type'] if e.get('type') else ''}] {label}{extra}")
        return {"url": self.url, "title": title, "text": text[: (20000 if full else 6000)], "elements": els}

    def _el(self, ref: str) -> dict:
        ref = ref.lstrip("@")
        for e in self.elements:
            if e["ref"] == ref:
                return e
        raise ToolError(f"no element {ref}; take a new snapshot")

    def click(self, ref: str) -> dict:
        e = self._el(ref)
        if e["kind"] == "link":
            return self.navigate(e["href"])
        if e["kind"] == "submit" and "form" in e:
            form = self.forms[e["form"]]
            fields = []
            for f in form["fields"]:
                if not f.get("name"):
                    continue
                if f["kind"] == "submit" and f is not e:
                    continue
                if f.get("type") in ("checkbox", "radio") and not f.get("checked"):
                    continue
                fields.append((f["name"], f.get("value", "")))
            data = urllib.parse.urlencode(fields)
            if form["method"] == "post":
                self._load(form["action"], data.encode(), "POST")
            else:
                sep = "&" if "?" in form["action"] else "?"
                self._load(form["action"].split("#")[0] + sep + data)
            return self.snapshot()
        if e.get("type") in ("checkbox", "radio"):
            e["checked"] = not e.get("checked")
            return {"ok": True, "checked": e["checked"]}
        raise ToolError("element is not clickable in lite mode (no JavaScript)")

    def type(self, ref: str, text: str, submit: bool = False) -> dict:
        e = self._el(ref)
        if e["kind"] == "select":
            if e["options"] and text not in e["options"]:
                raise ToolError(f"option {text!r} not in {e['options']}")
        elif e["kind"] != "input":
            raise ToolError("element is not a text field")
        e["value"] = text
        if submit and "form" in e:
            sub = next((f for f in self.forms[e["form"]]["fields"] if f["kind"] == "submit"), None)
            if sub:
                return self.click(sub["ref"])
            form = self.forms[e["form"]]
            fields = [(f["name"], f.get("value", "")) for f in form["fields"] if f.get("name") and f["kind"] != "submit"]
            self._load(form["action"] + "?" + urllib.parse.urlencode(fields))
            return self.snapshot()
        return {"ok": True, "ref": e["ref"], "value": text}

    def back(self) -> dict:
        if not self.history:
            raise ToolError("no history")
        url = self.history.pop()
        self.url = ""  # so _load does not push the page we are leaving
        self._load(url)
        return self.snapshot()

    def screenshot(self) -> bytes | None:
        return None

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- playwright engine

_SNAPSHOT_JS = r"""
() => {
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[contenteditable=true],summary';
  const out = []; let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    i += 1; el.setAttribute('data-hermes-ref', 'e' + i);
    const label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.name || el.href || '').trim().slice(0, 100);
    out.push('@e' + i + ' [' + el.tagName.toLowerCase() + (el.type ? ':' + el.type : '') + '] ' + label);
    if (i >= 250) break;
  }
  return {elements: out, text: document.body ? document.body.innerText : ''};
}
"""


class PlaywrightBrowser:
    """All Playwright calls run on one dedicated thread (sync API is thread-bound)."""

    def __init__(self, headless: bool = True, timeout: int = 30):
        self.q: queue.Queue = queue.Queue()
        self.headless = headless
        self.timeout = timeout * 1000
        self.page: Any = None
        self.ready = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(60)
        if self.error:
            raise ToolError(f"playwright failed to start: {self.error}")

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=self.headless)
            self.page = browser.new_page()
            self.page.set_default_timeout(self.timeout)
        except Exception as exc:
            self.error = exc
            self.ready.set()
            return
        self.ready.set()
        while True:
            fn, box, done = self.q.get()
            if fn is None:
                browser.close()
                pw.stop()
                return
            try:
                box["result"] = fn(self.page)
            except Exception as exc:
                box["error"] = exc
            done.set()

    def call(self, fn: Callable) -> Any:
        box: dict = {}
        done = threading.Event()
        self.q.put((fn, box, done))
        done.wait(self.timeout / 1000 + 30)
        if "error" in box:
            raise ToolError(str(box["error"]))
        return box.get("result")

    def snapshot(self, full: bool = False) -> dict:
        def fn(page):
            data = page.evaluate(_SNAPSHOT_JS)
            return {"url": page.url, "title": page.title(), "text": data["text"][: (20000 if full else 6000)], "elements": data["elements"]}
        return self.call(fn)

    def navigate(self, url: str) -> dict:
        if is_private_url(url):
            raise ToolError("refusing to browse a private/internal address")
        self.call(lambda p: p.goto(url, wait_until="domcontentloaded"))
        return self.snapshot()

    def click(self, ref: str) -> dict:
        ref = ref.lstrip("@")
        self.call(lambda p: p.click(f'[data-hermes-ref="{ref}"]'))
        self.call(lambda p: p.wait_for_load_state("domcontentloaded"))
        return self.snapshot()

    def type(self, ref: str, text: str, submit: bool = False) -> dict:
        ref = ref.lstrip("@")

        def fn(page):
            loc = page.locator(f'[data-hermes-ref="{ref}"]')
            if loc.evaluate("e => e.tagName.toLowerCase()") == "select":
                loc.select_option(text)
            else:
                loc.fill(text)
            if submit:
                loc.press("Enter")
        self.call(fn)
        return self.snapshot() if submit else {"ok": True, "ref": ref}

    def press(self, key: str) -> dict:
        self.call(lambda p: p.keyboard.press(key))
        return self.snapshot()

    def scroll(self, direction: str) -> dict:
        dy = 800 if direction == "down" else -800
        self.call(lambda p: p.mouse.wheel(0, dy))
        return self.snapshot()

    def back(self) -> dict:
        self.call(lambda p: p.go_back())
        return self.snapshot()

    def screenshot(self) -> bytes | None:
        return self.call(lambda p: p.screenshot(full_page=False))

    def close(self) -> None:
        self.q.put((None, None, None))


_sessions: dict[str, Any] = {}
_lock = threading.Lock()


def _playwright_ok() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def get_browser(ctx: ToolContext):
    key = ctx.task_id or ctx.session_id or "default"
    with _lock:
        if key not in _sessions:
            bcfg = ctx.config.get("browser", {}) or {}
            engine = bcfg.get("engine", "auto")
            if engine == "playwright" or (engine == "auto" and _playwright_ok()):
                try:
                    _sessions[key] = PlaywrightBrowser(bcfg.get("headless", True), int(bcfg.get("timeout", 30)))
                except ToolError:
                    if engine == "playwright":
                        raise
                    _sessions[key] = LiteBrowser()
            else:
                _sessions[key] = LiteBrowser()
        return _sessions[key]


def close_browser(key: str) -> None:
    with _lock:
        b = _sessions.pop(key, None)
    if b:
        b.close()


_REF = {"type": "string", "description": "Element ref from the latest snapshot, e.g. @e3"}


@tool("browser_navigate", "browser", "Open a URL in the browser and return a snapshot of the page with element refs.",
      {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, parallel_safe=False)
def browser_navigate(args, ctx):
    url = args["url"] if args["url"].startswith(("http://", "https://")) else "https://" + args["url"]
    return get_browser(ctx).navigate(url)


@tool("browser_snapshot", "browser", "Return the current page text and interactive elements (refs like @e1).",
      {"type": "object", "properties": {"full": {"type": "boolean", "default": False}}}, parallel_safe=False)
def browser_snapshot(args, ctx):
    return get_browser(ctx).snapshot(bool(args.get("full")))


@tool("browser_click", "browser", "Click an element by ref (links, buttons, submit inputs, checkboxes).",
      {"type": "object", "properties": {"ref": _REF}, "required": ["ref"]}, parallel_safe=False)
def browser_click(args, ctx):
    return get_browser(ctx).click(args["ref"])


@tool("browser_type", "browser", "Fill a text field / choose a select option by ref. submit=true presses Enter / submits the form.",
      {"type": "object", "properties": {"ref": _REF, "text": {"type": "string"}, "submit": {"type": "boolean", "default": False}},
       "required": ["ref", "text"]}, parallel_safe=False)
def browser_type(args, ctx):
    return get_browser(ctx).type(args["ref"], args["text"], bool(args.get("submit")))


@tool("browser_press", "browser", "Press a keyboard key (Enter, Escape, ArrowDown...). Playwright engine only.",
      {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}, parallel_safe=False)
def browser_press(args, ctx):
    b = get_browser(ctx)
    if not hasattr(b, "press"):
        raise ToolError("key presses need the playwright engine")
    return b.press(args["key"])


@tool("browser_scroll", "browser", "Scroll the page up or down. Playwright engine only.",
      {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down"]}}, "required": ["direction"]}, parallel_safe=False)
def browser_scroll(args, ctx):
    b = get_browser(ctx)
    if not hasattr(b, "scroll"):
        return b.snapshot(full=True)
    return b.scroll(args["direction"])


@tool("browser_back", "browser", "Go back to the previous page.", {"type": "object", "properties": {}}, parallel_safe=False)
def browser_back(args, ctx):
    return get_browser(ctx).back()


@tool("browser_vision", "browser", "Take a screenshot of the current page and ask a vision model a question about it "
      "(CAPTCHAs, charts, layout). Playwright engine only.",
      {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}, parallel_safe=False)
def browser_vision(args, ctx):
    shot = get_browser(ctx).screenshot()
    if not shot:
        raise ToolError("screenshots need the playwright engine")
    from hermes.tools.media import ask_vision

    url = "data:image/png;base64," + base64.b64encode(shot).decode()
    return {"answer": ask_vision(ctx, url, args["question"])}


@tool("browser_close", "browser", "Close the browser session.", {"type": "object", "properties": {}}, parallel_safe=False)
def browser_close(args, ctx):
    close_browser(ctx.task_id or ctx.session_id or "default")
    return {"closed": True}
