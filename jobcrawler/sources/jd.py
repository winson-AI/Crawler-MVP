from ..models import build_job
from .base import JobPage
from .common import HttpClient, iso_date, locations, plain_text


def normalize_job(raw):
    return build_job(
        id=raw.get("id") or raw.get("requirementId"), title=raw.get("positionNameOpen") or raw.get("positionName"),
        source="jd", company="京东", url=JdSource.url,
        locations=locations(raw.get("workCity")), requirements=plain_text(raw.get("qualification")),
        description=plain_text(raw.get("workContent")), published_at=iso_date(raw.get("publishTime")),
        category=raw.get("jobType") or "", department=raw.get("positionDeptName") or "",
    )


class JdSource:
    id, name, url = "jd", "京东社会招聘", "https://zhaopin.jd.com/web/job/job_info_list/3"
    page_size = 100

    def fetch_page(self, page):
        client = HttpClient(self.name, self.url)
        filters = {"workCityJson": "[]", "jobTypeJson": "[]", "jobSearch": "", "depTypeJson": "[]"}
        total = client.request("https://zhaopin.jd.com/web/job/job_count", filters, form=True)
        rows = client.request("https://zhaopin.jd.com/web/job/job_list", {
            **filters, "pageIndex": page, "pageSize": self.page_size,
        }, form=True)
        if not isinstance(rows, list):
            raise RuntimeError("京东接口结构变化：职位列表不是数组")
        return JobPage(page, int(total), [normalize_job(raw) for raw in rows], has_more=page * self.page_size < int(total))
