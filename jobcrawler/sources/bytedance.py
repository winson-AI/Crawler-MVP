from urllib.parse import urlencode

from ..models import build_job
from .base import JobPage
from .browser import BrowserSource
from .common import iso_date, plain_text, require_list


def normalize_job(raw):
    job_id = str(raw.get("id") or "")
    cities = raw.get("city_list") or ([raw["city_info"]] if raw.get("city_info") else [])
    return build_job(
        id=job_id, title=raw.get("title"), source="bytedance", company="字节跳动",
        url=f"https://jobs.bytedance.com/experienced/position/{job_id}/detail",
        locations=list(dict.fromkeys(city.get("i18n_name") or city.get("name") for city in cities)),
        requirements=plain_text(raw.get("requirement")), description=plain_text(raw.get("description")),
        published_at=iso_date(raw.get("publish_time")),
        category=(raw.get("job_category") or {}).get("i18n_name") or "",
    )


class BytedanceSource(BrowserSource):
    id, name, url = "bytedance", "字节跳动社会招聘", "https://jobs.bytedance.com/experienced/position"
    # The official page accepts limit=100 and emits the matching normal API request.
    page_size = 100

    def fetch_page(self, page):
        if self._context is None:
            with self.session():
                return self.fetch_page(page)
        offset = (page - 1) * self.page_size
        payload = self.load_response(self.url + "?" + urlencode({"current": page, "limit": self.page_size}), "/api/v1/search/job/posts",
                                     request_matches=lambda request: (request.post_data_json or {}).get("offset") == offset)
        if payload.get("code") != 0:
            raise RuntimeError(f"字节官网拒绝请求: {payload.get('message', '')}")
        data = payload.get("data")
        rows = require_list(data, "job_post_list", self.name)
        for raw in rows:
            recruit_type = raw.get("recruit_type") or {}
            if str((recruit_type.get("parent") or recruit_type).get("id")) != "1":
                raise RuntimeError("字节官网返回了非社招职位")
        return JobPage(page, int(data["count"]), [normalize_job(raw) for raw in rows],
                       has_more=offset + len(rows) < int(data["count"]))
