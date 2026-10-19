import time
from urllib.parse import urlencode, urlsplit

from ..models import build_job
from .base import JobPage
from .browser import BrowserSource
from .common import iso_date, locations, plain_text, require_list
from ..verification import VerificationRequired


def normalize_job(raw):
    return build_job(
        id=raw.get("code"), title=raw.get("name"), source="pinduoduo", company="拼多多集团",
        url="https://careers.pddglobalhr.com/jobs/detail?" + urlencode({"code": raw.get("code") or ""}),
        locations=locations(raw.get("workLocation")), requirements=plain_text(raw.get("serveRequirement")),
        description=plain_text(raw.get("jobDuty")), updated_at=iso_date(raw.get("updateTime")),
        category=raw.get("recruitType") or "",
    )


class PinduoduoSource(BrowserSource):
    id, name, url = "pinduoduo", "拼多多集团社会招聘", "https://careers.pddglobalhr.com/jobs"
    _current_page = 0
    _detail_page = None
    verification_timeout = 180
    verification_callback = None
    requires_listing_replay = True

    def launch_browser(self, playwright):
        # The public site rejects Chromium's default WebDriver context with 400023.
        # Use a separate ordinary Chrome window; no user profile or saved login is read.
        self._detail_page = None
        return playwright.chromium.launch(channel="chrome", headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"])

    def _load_operation(self, page, endpoint, operation, action):
        # The SDK can retry before navigation finishes. Subscribe before the
        # action so an already-arrived success is not mistaken for a timeout.
        responses = []

        def matches(response):
            return (urlsplit(response.url).path == endpoint
                    and all((response.request.post_data_json or {}).get(key) == value
                            for key, value in operation.items()))

        def record(response):
            if matches(response):
                responses.append(response)

        page.on("response", record)
        try:
            with page.expect_response(matches, timeout=45000) as pending:
                action()
            response = responses.pop(0) if responses else pending.value
            return self.read_response(page, response, endpoint, pending_responses=responses)
        finally:
            page.remove_listener("response", record)

    def read_response(self, page, response, endpoint, pending_responses=None):
        payload = self.response_payload(response)
        if payload.get("errorCode") == 54001:
            # The first response is a verification request with empty job fields.
            # Keep the page alive so the official SDK can finish its verification flow.
            # If it shows a CAPTCHA, the user completes it in Chrome; never solve it here.
            from playwright.sync_api import TimeoutError as BrowserTimeout
            print(f"拼多多：等待官网自动验证；若出现交互验证码请手动完成（最多等待 {self.verification_timeout} 秒）。", flush=True)
            body = response.request.post_data_json or {}
            operation = {key: body[key] for key in ("code", "page") if key in body}
            deadline = time.monotonic() + self.verification_timeout
            notified = False
            try:
                while payload.get("errorCode") == 54001:
                    if pending_responses:
                        payload = self.response_payload(pending_responses.pop(0))
                        continue
                    if (self.verification_callback and not notified
                            and page.get_by_placeholder("请输入上图计算结果", exact=True).is_visible() is True):
                        page.bring_to_front()
                        self.verification_callback(page, endpoint, operation, "needs_human")
                        notified = True
                    remaining = (deadline - time.monotonic()) * 1000
                    if remaining <= 0:
                        raise BrowserTimeout("verification deadline reached")
                    try:
                        with page.expect_response(lambda result:
                                urlsplit(result.url).path == endpoint
                                and all((result.request.post_data_json or {}).get(key) == value for key, value in operation.items()),
                                timeout=min(remaining, 1000) if self.verification_callback else remaining) as pending:
                            pass
                    except BrowserTimeout:
                        if self.verification_callback and time.monotonic() < deadline:
                            continue
                        raise
                    response = pending_responses.pop(0) if pending_responses else pending.value
                    payload = self.response_payload(response)
            except BrowserTimeout as exc:
                evidence = self.verification_callback(page, endpoint, operation, "needs_human" if notified else "unknown") if self.verification_callback else None
                raise VerificationRequired("拼多多官网验证响应超时；如有验证码，请在 Chrome 手动完成后恢复；保留旧缓存", evidence) from exc
            if notified and payload.get("success") is True:
                self.verification_callback(page, endpoint, operation, "cleared")
        return payload

    def fetch_detail(self, code):
        # Verification state is in sessionStorage: keep this tab across job navigations.
        if self._detail_page is None:
            self._detail_page = self._context.new_page()
        page = self._detail_page
        endpoint = "/api/recruit/position/detail"
        return self._load_operation(page, endpoint, {"code": code}, lambda:
            page.goto(self.url + "/detail?" + urlencode({"code": code}), wait_until="domcontentloaded", timeout=45000))

    def fetch_list_page(self, page):
        if self._context is None:
            with self.session():
                return self.fetch_list_page(page)

        if page == 1:
            if self._listing_page:
                self._listing_page.close()
            self._listing_page = self._context.new_page()
            self._current_page = 0
        if self._listing_page is None or page != self._current_page + 1:
            raise RuntimeError("拼多多浏览器分页必须从 1 开始依次抓取")
        browser_page = self._listing_page
        def navigate():
            if page == 1:
                browser_page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
            else:
                browser_page.get_by_title("下一页", exact=True).click()
        payload = self._load_operation(browser_page, "/api/recruit/position/list", {"page": page}, navigate)
        if payload.get("success") is not True:
            raise RuntimeError(f"拼多多官网获取失败: {payload.get('errorCode')} / {payload.get('errorMsg')}；保留旧缓存")
        data = payload.get("result")
        rows = require_list(data, "list", self.name)
        self._current_page = page
        return JobPage(page, int(data["total"]), [normalize_job(raw) for raw in rows], has_more=page * 10 < int(data["total"]))

    def fetch_job_detail(self, job):
        detail = self.fetch_detail(job["id"])
        if detail.get("success") is not True or not isinstance(detail.get("result"), dict):
            raise RuntimeError(f"拼多多职位详情获取失败: {job['id']} / {detail.get('errorCode')} / {detail.get('errorMsg')}")
        raw = {"code": job["id"], "name": job["title"], "workLocation": "/".join(job["locations"]),
               "updateTime": job["updated_at"], "recruitType": job["category"]}
        merged = {**raw, **detail["result"]}
        if merged.get("code") != job["id"]:
            raise RuntimeError("拼多多详情 ID 与列表不一致")
        return normalize_job(merged)

    def fetch_page(self, page):
        if self._context is None:
            with self.session():
                return self.fetch_page(page)
        listing = self.fetch_list_page(page)
        jobs = []
        for index, job in enumerate(listing.jobs):
            if index:
                time.sleep(0.1)
            jobs.append(self.fetch_job_detail(job))
        listing.jobs = jobs
        return listing
