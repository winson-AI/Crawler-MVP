import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..models import build_job
from .base import JobPage

SOURCE_URL = "https://talent.baidu.com/jobs/social-list"
API_URL = "https://talent.baidu.com/httservice/getPostListNew"


def normalize_job(raw):
    job_id = str(raw.get("postId") or "")
    return build_job(
        id=job_id, title=raw.get("name"), source="baidu", company="百度",
        url=f"https://talent.baidu.com/jobs/detail/SOCIAL/{job_id}",
        locations=[s.strip() for s in re.split(r"[,，、;；]", raw.get("workPlace") or "") if s.strip()],
        requirements=str(raw.get("serviceCondition") or ""),
        description=str(raw.get("workContent") or ""), work_years=raw.get("workYears"),
        published_at=raw.get("publishDate") or "", updated_at=raw.get("updateDate") or "",
        category=raw.get("postType") or "", department=raw.get("bgShortName") or "",
    )


class BaiduSource:
    id = "baidu"
    name = "百度社会招聘"
    url = SOURCE_URL

    def fetch_page(self, page, timeout=20):
        body = urlencode({"recruitType": "SOCIAL", "curPage": page, "pageSize": 20,
                          "keyWord": "", "projectType": ""}).encode()
        request = Request(API_URL, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Referer": self.url, "User-Agent": "JobCrawlerMVP/0.1 (public recruitment data)",
        })
        for attempt in range(3):
            try:
                with urlopen(request, timeout=timeout) as response:
                    payload = json.load(response)
                break
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError(f"百度接口 HTTP {exc.code}") from exc
                time.sleep(2 ** attempt)
            except (URLError, TimeoutError) as exc:
                if attempt == 2:
                    raise RuntimeError(f"无法连接百度招聘: {exc}") from exc
                time.sleep(2 ** attempt)
        if payload.get("status") != "ok":
            raise RuntimeError(f"百度接口拒绝请求: {payload.get('status')} / {payload.get('message', '')}")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RuntimeError("百度接口结构变化：缺少 data.list")
        return JobPage(page=int(data["pageNum"]), total=int(data["total"]),
                       jobs=[normalize_job(raw) for raw in data["list"]])
