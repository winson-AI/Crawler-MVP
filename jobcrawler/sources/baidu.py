import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..models import build_job
from ..fields import source_fields
from .base import JobPage

SOURCE_URL = "https://talent.baidu.com/jobs/social-list"
API_URL = "https://talent.baidu.com/httservice/getPostListNew"


def normalize_job(raw):
    job_id = str(raw.get("postId") or "")
    return build_job(
        **source_fields(raw),
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
        page_num, total = int(data["pageNum"]), int(data["total"])
        has_more = None
        if "pages" in data and "pageSize" in data:
            page_count, page_size = int(data["pages"]), int(data["pageSize"])
            if (page_size <= 0 or total < 0 or page_count != (total + page_size - 1) // page_size
                    or page_num < 1 or page_num > max(1, page_count)):
                raise RuntimeError("百度分页元数据不一致，保留旧缓存")
            expected = min(page_size, max(0, total - (page_num - 1) * page_size))
            if len(data["list"]) != expected:
                raise RuntimeError("百度分页条数与总数不一致，保留旧缓存")
            # PageInfo's hasNextPage/isLastPage flags are unset on this API.
            # Explicit page count + exact row counts define the listing boundary.
            has_more = page_num < page_count
        return JobPage(page=page_num, total=total,
                       jobs=[normalize_job(raw) for raw in data["list"]], has_more=has_more)
