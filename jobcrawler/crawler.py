import csv
import io
import json
import os
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from .sources import get_source

DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "data" / "jobs.json"


class PaginationError(RuntimeError):
    def __init__(self, message, page):
        super().__init__(message)
        self.page = page


def cache_path(source="baidu", base=DEFAULT_CACHE):
    get_source(source)
    base = Path(base)
    return base if source == "baidu" else base.with_name(f"{base.stem}.{source}{base.suffix}")


def crawl(max_pages=0, delay=0.4, progress=None, fetch=None, source="baidu"):
    adapter = get_source(source)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 0 <= max_pages <= 1000:
        raise ValueError("max_pages 必须是 0–1000 的整数（0 为全量）")
    if not 0.1 <= delay <= 60:
        raise ValueError("抓取间隔必须为 0.1–60 秒")
    # Browser adapters share one ordinary browser session across pages, closed on failure too.
    session = adapter.session() if fetch is None and hasattr(adapter, "session") else nullcontext()
    with session:
        return _crawl_pages(adapter, fetch or adapter.fetch_page, max_pages, delay, progress)


def _crawl_pages(adapter, fetch, max_pages, delay, progress):
    jobs, seen, page, scanned = [], set(), 1, 0
    while True:
        data = fetch(page)
        total = data.total
        if total < 0 or data.page != page:
            raise RuntimeError("接口返回了无效总数或错误页码")
        rows = data.jobs
        scanned += len(rows)
        added = 0
        for job in rows:
            if job["source"] != adapter.id:
                raise ValueError("适配器返回的职位 source 与数据源不一致")
            if job["id"] not in seen:
                seen.add(job["id"])
                jobs.append(job)
                added += 1
        if progress:
            progress({"page": page, "collected": len(jobs), "reported_total": total})
        complete = ((data.has_more is False and (bool(rows) or total == 0))
                    or (data.has_more is None and len(jobs) >= total))
        if complete:
            break
        if not rows or not added:
            raise PaginationError(f"分页未前进：第 {page} 页，仅收集 {len(jobs)}/{total} 条；保留旧缓存", page)
        if max_pages and page >= max_pages:
            break
        if page >= 1000:
            raise RuntimeError("超过 1000 页，停止抓取；保留旧缓存")
        time.sleep(delay)
        page += 1
    return {"source_id": adapter.id, "source_name": adapter.name, "source_url": adapter.url, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "reported_total": total, "collected": len(jobs), "pages": page,
            "scanned": scanned, "duplicates": scanned - len(jobs),
            "complete": complete, "jobs": jobs}


def save_cache(snapshot, path=DEFAULT_CACHE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_cache(path=DEFAULT_CACHE, source="baidu"):
    adapter = get_source(source)
    path = Path(path)
    if path.exists():
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if snapshot.get("source_id", "baidu") != source:
            raise ValueError("缓存数据源与请求的数据源不一致")
        snapshot.setdefault("source_id", adapter.id)
        snapshot.setdefault("source_name", adapter.name)
        # Old snapshots lack publication dates: never infer them from updated_at.
        for job in snapshot["jobs"]:
            job.setdefault("published_at", "")
        return snapshot
    return {"source_id": adapter.id, "source_name": adapter.name, "source_url": adapter.url,
            "fetched_at": None, "reported_total": 0, "collected": 0, "pages": 0,
            "complete": False, "jobs": []}


def export_csv(jobs):
    stream = io.StringIO(newline="")
    fields = ["title", "source", "company", "locations", "experience_min", "experience_max", "experience_evidence",
              "technologies", "department", "category", "published_at", "updated_at", "url", "requirements", "description"]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for job in jobs:
        row = {key: job.get(key, "") for key in fields}
        row.update(locations=", ".join(job["locations"]), technologies=", ".join(job["technologies"]),
                   experience_min=job["experience"]["min"], experience_max=job["experience"]["max"],
                   experience_evidence=job["experience"]["evidence"])
        # Avoid spreadsheet formula execution when opening text from remote postings.
        writer.writerow({key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                         else value for key, value in row.items()})
    return "\ufeff" + stream.getvalue()
