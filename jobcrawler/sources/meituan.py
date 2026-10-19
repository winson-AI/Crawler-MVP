from ..models import build_job
from .base import JobPage
from .common import HttpClient, iso_date, plain_text, require_list


def normalize_job(raw):
    job_id = str(raw.get("jobUnionId") or "")
    return build_job(
        id=job_id, title=raw.get("name"), source="meituan", company="美团",
        url=f"https://hr.meituan.com/web/position/detail?highlightType=social&jobUnionId={job_id}",
        locations=[city["name"] for city in raw.get("cityList") or []],
        requirements=plain_text(raw.get("jobRequirement")), description=plain_text(raw.get("jobDuty")),
        work_years=raw.get("workYear"), published_at=iso_date(raw.get("firstPostTime")),
        updated_at=iso_date(raw.get("refreshTime")), category=raw.get("jobFamily") or "",
        department=" / ".join(dept["name"] for dept in raw.get("department") or []),
    )


class MeituanSource:
    id, name, url = "meituan", "美团社会招聘", "https://hr.meituan.com/web/social"
    page_size = 10

    def fetch_page(self, page):
        payload = HttpClient(self.name, self.url).request("https://hr.meituan.com/api/official/job/getJobList", {
            "page": {"pageNo": page, "pageSize": self.page_size}, "jobShareType": "1", "keywords": "",
            "cityList": [], "department": [], "jfJgList": [], "jobType": [{"code": "3", "subCode": []}], "typeCode": [],
        })
        if payload.get("status") != 1 or not isinstance(payload.get("data"), dict):
            raise RuntimeError(f"美团拒绝请求: {payload.get('message', '')}")
        data = payload["data"]
        rows = require_list(data, "list", self.name)
        if any(str(raw.get("jobType")) != "3" for raw in rows):
            raise RuntimeError("美团返回了非社招职位")
        current, total = int(data["page"]["pageNo"]), int(data["page"]["totalCount"])
        return JobPage(current, total, [normalize_job(raw) for raw in rows], has_more=current * self.page_size < total)
