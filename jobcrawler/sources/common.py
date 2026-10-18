import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


class HttpClient:
    """Public recruitment requests with ordinary cookies and bounded retries."""
    def __init__(self, name, referer):
        self.name, self.referer = name, referer
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, url, data=None, form=False, as_json=True):
        headers = {"User-Agent": "Mozilla/5.0 (compatible; JobCrawler/0.2)", "Referer": self.referer}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            body = (urlencode(data) if form else json.dumps(data)).encode()
        for attempt in range(3):
            try:
                with self.opener.open(Request(url, data=body, headers=headers), timeout=25) as response:
                    text = response.read().decode("utf-8-sig")
                if not as_json:
                    return text
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise RuntimeError(f"{self.name}返回非 JSON 数据，可能需要登录/验证或接口已变化") from exc
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError(f"{self.name}接口 HTTP {exc.code}") from exc
            except (URLError, TimeoutError) as exc:
                if attempt == 2:
                    raise RuntimeError(f"无法连接{self.name}: {exc}") from exc
            time.sleep(2 ** attempt)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    text = str(value or "")
    # Keep technical strings such as C++ and generic type names in plain text.
    if not re.search(r"</?(?:p|br|div|li|span|ul|ol|b|strong)\b", text, re.I):
        return text.strip()
    parser = _Text()
    parser.feed(text)
    return "".join(parser.parts).strip()


def locations(value):
    return [part.strip() for part in re.split(r"[,，、;；/]+", value or "") if part.strip()]


def iso_date(value):
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 100000000000 else value
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    value = str(value).strip()
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
    if match:
        return "{}-{:02d}-{:02d}".format(*map(int, match.groups()))
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def require_list(data, field, name):
    if not isinstance(data, dict) or not isinstance(data.get(field), list):
        raise RuntimeError(f"{name}接口结构变化：缺少 {field}")
    return data[field]
