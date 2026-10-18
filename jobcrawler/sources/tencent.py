import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from ..models import build_job
from .base import JobPage
from .common import HttpClient, iso_date, locations, plain_text, require_list


def normalize_job(raw):
    job_id = str(raw.get("PostId") or "")
    return build_job(
        id=job_id, title=raw.get("RecruitPostName"), source="tencent", company="腾讯",
        url=f"https://careers.tencent.com/jobdesc.html?postId={job_id}",
        locations=locations(raw.get("LocationName")), requirements=plain_text(raw.get("Requirement")),
        description=plain_text(raw.get("Responsibility")), work_years=raw.get("RequireWorkYearsName"),
        updated_at=iso_date(raw.get("LastUpdateTime")), category=raw.get("CategoryName") or "",
        department=raw.get("BGName") or "",
    )


class TencentSource:
    id, name, url = "tencent", "腾讯社会招聘", "https://careers.tencent.com/search.html"
    api = "https://careers.tencent.com/tencentcareer/api/post/"
    page_size = 100

    def fetch_page(self, page):
        client = HttpClient(self.name, self.url)
        payload = client.request(self.api + "Query?" + urlencode({
            "pageIndex": page, "pageSize": self.page_size, "language": "zh-cn", "attrId": 1,
        }))
        if payload.get("Code") != 200:
            raise RuntimeError("腾讯拒绝职位列表请求")
        data = payload.get("Data")
        rows = require_list(data, "Posts", self.name)
        def fetch_detail(raw):
            time.sleep(0.1)
            # Each worker owns its HTTP client; at most eight public detail requests in flight.
            detail = HttpClient(self.name, self.url).request(self.api + "ByPostId?" + urlencode({"postId": raw["PostId"], "language": "zh-cn"}))
            if detail.get("Code") != 200 or not isinstance(detail.get("Data"), dict):
                raise RuntimeError(f"腾讯职位详情获取失败: {raw['PostId']}")
            if str(detail["Data"].get("PostId")) != str(raw["PostId"]):
                raise RuntimeError("腾讯详情 ID 与列表不一致")
            return normalize_job({**raw, **detail["Data"]})
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(fetch_detail, rows))
        return JobPage(page, int(data["Count"]), jobs, has_more=page * self.page_size < int(data["Count"]))
