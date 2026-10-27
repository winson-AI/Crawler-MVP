import re
from urllib.parse import urlencode

from ..models import build_job
from ..fields import source_fields
from .base import JobPage
from .common import HttpClient, iso_date, plain_text, require_list


def normalize_job(raw):
    job_id = str(raw.get("id") or "")
    years = raw.get("experience") or {}
    experience = None
    if years.get("from") is not None or years.get("to") is not None:
        low, high = years.get("from"), years.get("to")
        experience = {"min": low, "max": high, "source": "structured",
                      "evidence": f"{low}–{high}年" if low is not None and high is not None else f"{low}年以上" if low is not None else f"{high}年以下"}
    return build_job(
        **source_fields(raw),
        id=job_id, title=raw.get("name"), source="alibaba", company="阿里巴巴集团",
        url=f"https://talent.alibaba.com/off-campus/position-detail?positionId={job_id}",
        locations=raw.get("workLocations") or [], requirements=plain_text(raw.get("requirement")),
        description=plain_text(raw.get("description")), published_at=iso_date(raw.get("publishTime")),
        updated_at=iso_date(raw.get("modifyTime")), category=" / ".join(raw.get("categories") or []),
        experience=experience,
    )


class AlibabaSource:
    id, name, url = "alibaba", "阿里巴巴集团社会招聘", "https://talent.alibaba.com/"
    page_size = 100

    def fetch_page(self, page):
        client = HttpClient(self.name, self.url)
        html = client.request(self.url, as_json=False)
        token = re.search(r'__token__\s*:\s*["\']([^"\']+)', html)
        channel = re.search(r'offCampus:\s*["\']([^"\']+)', html)
        if not token or not channel:
            raise RuntimeError("阿里官网结构变化：缺少公开页面 CSRF 或社招 channel 配置")
        payload = client.request(self.url + "position/search?" + urlencode({"_csrf": token.group(1)}), {
            "pageIndex": page, "pageSize": self.page_size, "channel": channel.group(1), "language": "zh", "key": "",
            "categories": "", "regions": "", "subCategories": "", "deptCodes": [], "batchId": "",
        })
        if payload.get("success") is not True:
            raise RuntimeError(f"阿里拒绝请求: {payload.get('errorMsg', '')}")
        data = payload.get("content")
        rows = require_list(data, "datas", self.name)
        return JobPage(int(data["currentPage"]), int(data["totalCount"]), [normalize_job(raw) for raw in rows],
                       has_more=int(data["currentPage"]) * int(data.get("pageSize", self.page_size)) < int(data["totalCount"]))
