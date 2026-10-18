from contextlib import contextmanager
from urllib.parse import urlsplit


class BrowserSource:
    """Read responses emitted by the normal public website; never forge signatures."""
    _browser = None
    _context = None
    _listing_page = None

    def launch_browser(self, playwright):
        return playwright.chromium.launch(headless=True)

    @contextmanager
    def session(self):
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError as exc:
            raise RuntimeError(f"{self.name}需要浏览器：先运行 pip install -r requirements.txt，再运行 python -m playwright install chromium") from exc
        with sync_playwright() as playwright:
            try:
                self._browser = self.launch_browser(playwright)
                self._context = self._browser.new_context()
                self._listing_page = None
                yield
            except Error as exc:
                raise RuntimeError(f"{self.name}官网浏览器读取失败；可能需要验证或页面已变化: {exc}") from exc
            finally:
                if self._browser:
                    self._browser.close()
                self._browser = self._context = None
                self._listing_page = None

    def load_response(self, url, endpoint, action=None, request_matches=None):
        page = self._context.new_page()
        try:
            def matches(response):
                return (urlsplit(response.url).path == endpoint
                        and response.request.resource_type in ("xhr", "fetch")
                        and (request_matches is None or request_matches(response.request)))

            with page.expect_response(matches, timeout=45000) as pending:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if action:
                    action(page)
            return self.read_response(page, pending.value, endpoint)
        finally:
            page.close()

    def read_response(self, page, response, endpoint):
        return self.response_payload(response)

    def response_payload(self, response):
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.name}官网返回非 JSON，可能需要验证码或登录") from exc
        # Some sites return a useful application error even with HTTP 400.
        if response.status >= 400:
            message = payload.get("errorMsg") or payload.get("message") or "请求被拒绝"
            code = payload.get("errorCode", "")
            raise RuntimeError(f"{self.name}官网 HTTP {response.status} / {code}: {message}；保留旧缓存")
        return payload
