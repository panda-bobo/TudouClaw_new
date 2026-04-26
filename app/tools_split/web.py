"""Network tools — web_search / web_fetch / web_screenshot / http_request.

All four handlers deal with outbound HTTP. ``web_screenshot`` additionally
shells out to Playwright / wkhtmltoimage / cutycapt when present.

Schemas still live in ``tools.TOOL_DEFINITIONS``; only handlers moved.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json as _json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..defaults import MAX_HTTP_RESPONSE_CHARS

logger = logging.getLogger(__name__)


# Shared desktop User-Agent used across all outbound HTTP. Some sites
# return 403 for non-browser UAs; this one mimics a current Chrome on
# macOS which is the most permissive.
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Per-request timeouts, seconds. DDG instant-answer API is fast; HTML
# scraping fallback needs more headroom.
_DDG_API_TIMEOUT = 10
_DDG_HTML_TIMEOUT = 15
_FETCH_TIMEOUT = 20

# web_screenshot: headless-browser launch and fallback CLI timeouts.
_PLAYWRIGHT_TIMEOUT_MS = 30000
_PLAYWRIGHT_SUBPROCESS_TIMEOUT_S = 45
_FALLBACK_CLI_TIMEOUT_S = 30
_WHICH_PROBE_TIMEOUT_S = 5

# http_request: user cannot exceed 120 s to keep the agent responsive.
_HTTP_TIMEOUT_MAX_S = 120
_HTTP_TIMEOUT_MIN_S = 1

# Cap headers shown in the formatted http_request response — more than
# this is noise and blows up the agent's context window.
_HTTP_HEADERS_SHOWN = 20


# ── web_search ───────────────────────────────────────────────────────

def _tool_web_search(query: str, max_results: int = 8, **_: Any) -> str:
    """Search the internet using DuckDuckGo (API + HTML fallback)."""
    headers = {"User-Agent": _DESKTOP_UA}

    # ── Strategy 1: DuckDuckGo Instant Answer API (fast, structured) ──
    try:
        api_url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({
            "q": query, "format": "json", "no_html": "1",
            "skip_disambig": "1", "no_redirect": "1",
        })
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=_DDG_API_TIMEOUT) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        results = []
        # Abstract (direct answer)
        if data.get("Abstract"):
            results.append(
                f"0. {data.get('Heading', query)} (Direct Answer)\n"
                f"   URL: {data.get('AbstractURL', '')}\n"
                f"   {data['Abstract']}"
            )
        # Related topics
        for i, topic in enumerate(data.get("RelatedTopics", [])[:max_results]):
            if isinstance(topic, dict) and topic.get("Text"):
                url_ = topic.get("FirstURL", "")
                text = topic["Text"]
                results.append(f"{i+1}. {text[:120]}\n   URL: {url_}")
        if results:
            return f"Search results for: {query}\n\n" + "\n\n".join(results)
    except Exception:
        pass  # Fall through to HTML scraping

    # ── Strategy 2: DuckDuckGo HTML scraping (more results) ──
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url, headers=headers, method="POST",
        data=urllib.parse.urlencode({"q": query}).encode())
    try:
        with urllib.request.urlopen(req, timeout=_DDG_HTML_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"Error: Web search failed: {e}"

    # Parse results from DuckDuckGo HTML.
    results = []
    link_pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL
    )
    snippet_pattern = re.compile(
        r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
    )

    links = link_pattern.findall(body)
    snippets = snippet_pattern.findall(body)

    for i, (raw_url, raw_title) in enumerate(links[:max_results]):
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        title = html_mod.unescape(title)
        actual_url = raw_url
        m = re.search(r'uddg=([^&]+)', raw_url)
        if m:
            actual_url = urllib.parse.unquote(m.group(1))
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            snippet = html_mod.unescape(snippet)
        results.append(f"{i+1}. {title}\n   URL: {actual_url}\n   {snippet}")

    if not results:
        return "No search results found."
    return f"Search results for: {query}\n\n" + "\n\n".join(results)


# ── web_fetch ────────────────────────────────────────────────────────

def _tool_web_fetch(url: str, max_length: int = 5000, **_: Any) -> str:
    """Fetch the text content of a web page URL.

    Default max_length is 5000 chars (~1250 tokens). A typical research
    session does 3-6 fetches — at 10000 chars/fetch the history alone
    burned 25k+ tokens and crowded out the actual work. Callers that
    need more can pass max_length explicitly, but the default now
    favors breadth (more URLs visited) over depth per URL.
    """
    headers = {"User-Agent": _DESKTOP_UA}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            # Detect encoding from Content-Type header, default utf-8.
            encoding = "utf-8"
            if "charset=" in content_type:
                encoding = content_type.split("charset=")[-1].split(";")[0].strip()
            body = raw.decode(encoding, errors="replace")
    except Exception as e:
        return f"Error: Failed to fetch URL: {e}"

    # Strip HTML tags to get plain text.
    # Remove script and style blocks first (their content is never wanted).
    text = re.sub(r"<script[^>]*>.*?</script>", "", body,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text,
                  flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level tags with newlines so paragraphs stay readable.
    text = re.sub(r"<(?:br|p|div|li|tr|h[1-6])[^>]*>", "\n", text,
                  flags=re.IGNORECASE)
    # Remove remaining tags and decode HTML entities.
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    # Collapse whitespace.
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)

    if len(text) > max_length:
        text = text[:max_length] + f"\n\n... (truncated at {max_length} characters)"

    # ── Quality observation (Nov 2026) ───────────────────────────────
    # If fetched content looks like a login wall / paywall / near-empty
    # page, append an [OBSERVATION] block telling the agent what we see.
    # Agent decides whether to save_experience and remember the domain.
    # NO hardcoded blacklist — agent learns over time via experience lib,
    # which is auto-injected into same-role agents' system prompts.
    obs = _detect_low_value_content(text, url)
    if obs:
        text = text + "\n\n" + obs

    return f"[Content from {url}]\n\n{text}"


# ── Low-value page detector ──────────────────────────────────────────
# Heuristic only — no fixed domain list. Returns an [OBSERVATION] block
# that primes the agent to save_experience if it judges this domain
# fundamentally unusable for crawling. Pure "nudge", not enforcement.

_LOGIN_WALL_PHRASES = (
    "please log in", "sign in to continue", "log in to read",
    "login required", "subscribe to read", "subscription required",
    "为提供更好的浏览体验", "请登录后查看", "请登录后继续", "登录后访问",
    "需要登录后查看", "scan the qr code to log in",
)


def _detect_low_value_content(text: str, url: str) -> str:
    """Return an [OBSERVATION] block if fetched content looks unusable.

    Triggers:
      - text shorter than 400 chars (mostly nav / paywall)
      - any login-wall phrase in first 5KB
      - >=3 occurrences of "login"/"sign in"/"登录" in short content
    """
    if not text:
        return ""
    sample = text.lower()[:5000]
    n = len(text)

    triggers: list[str] = []

    if n < 400:
        triggers.append(f"内容长度仅 {n} 字符,可能是导航壳或登录墙")
    for phrase in _LOGIN_WALL_PHRASES:
        if phrase in sample:
            triggers.append(f"页面含 '{phrase}'")
            break
    if n < 1500:
        signin_count = (sample.count("sign in") + sample.count("login")
                        + sample.count("登录"))
        if signin_count >= 3:
            triggers.append(f"短内容中出现 {signin_count} 次登录提示")

    if not triggers:
        return ""

    try:
        domain = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        domain = url

    return (
        "[OBSERVATION — fetch 质量异常]\n"
        f"- URL: {url}\n"
        f"- 域名: {domain}\n"
        f"- 触发原因: {'; '.join(triggers)}\n"
        "\n"
        "如果你判断 **该域名总体不适合 web_fetch** (而非偶发),建议沉淀经验:\n"
        "  save_experience(\n"
        "    scene='使用 web_search/web_fetch 做调研',\n"
        f"    core_knowledge='{domain} 类站点对爬虫不友好/需登录/含付费墙',\n"
        f"    taboo_rules=['不要 web_fetch {domain} 下的页面,优先在搜索结果里跳过这个域'],\n"
        "    priority='medium'\n"
        "  )\n"
        "下次同 role 的所有 agent 都会自动收到这条经验,启动时注入 system prompt,\n"
        "再做 web_search 时看到这个域名你就会自然跳过。\n"
        "如果只是单次失败 (网络抖动/单页问题), 不必沉淀,换个 URL 即可。"
    )


# ── web_screenshot ───────────────────────────────────────────────────

def _tool_web_screenshot(url: str, output_path: str = "",
                         full_page: bool = False,
                         width: int = 1280, height: int = 720,
                         **_: Any) -> str:
    """Take a screenshot of a web page using Playwright or Selenium."""
    # Determine output path.
    if not output_path:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        ts = int(time.time())
        output_path = f"/tmp/screenshot_{url_hash}_{ts}.png"

    # Strategy 1: Try Playwright (preferred).
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(url, wait_until="networkidle",
                      timeout=_PLAYWRIGHT_TIMEOUT_MS)
            page.screenshot(path=output_path, full_page=full_page)
            browser.close()
        size = os.path.getsize(output_path)
        return f"Screenshot saved to {output_path} ({size} bytes, {width}x{height})"
    except ImportError:
        logger.debug("Playwright not installed, trying fallback methods")
    except Exception as e:
        # Playwright installed but failed; try fallback.
        logger.debug("Playwright screenshot failed: %s, trying fallback methods", e)

    # Strategy 2: Try subprocess with playwright CLI.
    try:
        cmd = (f'python3 -c "from playwright.sync_api import sync_playwright; '
               f'p=sync_playwright().start(); '
               f'b=p.chromium.launch(headless=True); '
               f"pg=b.new_page(viewport={{'width':{width},'height':{height}}}); "
               f"pg.goto('{url}',wait_until='networkidle',timeout={_PLAYWRIGHT_TIMEOUT_MS}); "
               f"pg.screenshot(path='{output_path}',full_page={full_page}); "
               f"b.close(); p.stop(); print('ok')\"")
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=_PLAYWRIGHT_SUBPROCESS_TIMEOUT_S)
        if result.returncode == 0 and os.path.exists(output_path):
            size = os.path.getsize(output_path)
            return f"Screenshot saved to {output_path} ({size} bytes, {width}x{height})"
    except Exception as e:
        logger.debug("Playwright subprocess failed: %s, trying other methods", e)

    # Strategy 3: Use cutycapt or wkhtmltoimage if available.
    for cmd_name, cmd_tpl in [
        ("wkhtmltoimage",
         f"wkhtmltoimage --width {width} --height {height} '{url}' '{output_path}'"),
        ("cutycapt",
         f"cutycapt --url='{url}' --out='{output_path}' "
         f"--min-width={width} --min-height={height}"),
    ]:
        try:
            result = subprocess.run(
                f"which {cmd_name}", shell=True, capture_output=True,
                timeout=_WHICH_PROBE_TIMEOUT_S)
            if result.returncode == 0:
                result = subprocess.run(
                    cmd_tpl, shell=True, capture_output=True, text=True,
                    timeout=_FALLBACK_CLI_TIMEOUT_S)
                if os.path.exists(output_path):
                    size = os.path.getsize(output_path)
                    return (f"Screenshot saved to {output_path} "
                            f"({size} bytes, {width}x{height})")
        except Exception:
            continue

    return (
        "Error: Screenshot tools not available. Please install one of:\n"
        "  pip install playwright && playwright install chromium\n"
        "  apt install wkhtmltopdf\n"
        "You can also use the 'browser' MCP (Puppeteer) for screenshots."
    )


# ── http_request ─────────────────────────────────────────────────────

def _tool_http_request(url: str, method: str = "GET",
                       headers: dict | None = None,
                       body: str = "", json_body: dict | None = None,
                       timeout: int = 30, **_: Any) -> str:
    """Make an HTTP request to any URL."""
    method = method.upper()
    req_headers = {"User-Agent": "TudouClaw-Agent/1.0"}
    if headers:
        req_headers.update(headers)

    data = None
    if json_body is not None:
        data = _json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif body:
        data = body.encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=req_headers,
                                 method=method)
    try:
        timeout = max(_HTTP_TIMEOUT_MIN_S,
                      min(int(timeout), _HTTP_TIMEOUT_MAX_S))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            resp_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = dict(e.headers) if hasattr(e, 'headers') else {}
        try:
            resp_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            resp_body = str(e)
    except Exception as e:
        return f"Error: HTTP request failed: {e}"

    # Format response.
    result = f"HTTP {status} {method} {url}\n"
    result += "--- Headers ---\n"
    for k, v in list(resp_headers.items())[:_HTTP_HEADERS_SHOWN]:
        result += f"  {k}: {v}\n"
    result += "--- Body ---\n"
    if len(resp_body) > MAX_HTTP_RESPONSE_CHARS:
        resp_body = (resp_body[:MAX_HTTP_RESPONSE_CHARS]
                     + f"\n... (truncated at {MAX_HTTP_RESPONSE_CHARS} chars, "
                     f"total: {len(resp_body)})")
    result += resp_body
    return result
