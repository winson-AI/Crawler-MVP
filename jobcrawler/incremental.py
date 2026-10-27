"""List-first acquisition, per-job detail caching, and resumable checkpoints."""
import hashlib
import json
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .crawler import PaginationError, load_cache, save_cache
from .selectors import job_recruitment_type
from .fields import comparable_job, normalize_fields
from .sources import get_source
from .sources.base import JobPage
from .verification import record_verification

LIST_FIELDS = ("id", "source", "title", "url", "locations", "published_at", "updated_at", "category", "department")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def content_hash(job, source_url="", ignore_metadata=False):
    normalized = {**job, "recruitment_type": job_recruitment_type(job, source_url)}
    content = comparable_job(normalized)
    if not ignore_metadata:
        content["updated_at"] = job.get("updated_at", "")
    return digest(content)


def collect_incremental(source, previous, directory, details, crawl_fn, delay=0.4,
                        progress=None, on_verification=None, verification_wait=180,
                        defer_details=False):
    adapter = get_source(source)
    delay = max(delay, getattr(adapter, "minimum_delay", 0))
    split = callable(getattr(adapter, "fetch_list_page", None)) and callable(getattr(adapter, "fetch_job_detail", None))
    directory, details = Path(directory), Path(details) / source
    directory.mkdir(parents=True, exist_ok=True)
    before_path = directory / "before.json"
    if before_path.exists():
        previous = load_cache(before_path, source)
    else:
        save_cache(previous, before_path)
    stats = {"listing_network_pages": 0, "listing_cache_pages": 0, "listing_replayed_pages": 0,
             "listing_restarts": 0, "detail_requests": 0, "detail_cache_hits": 0, "legacy_cache_hits": 0,
             "embedded_details": 0, "request_delay": delay, "details_deferred": 0, "listing_setup_requests": 0,
             "listing_page_reused": False, "verification_handoffs": 0, "verification_cleared": 0}
    previous_jobs = {str(j["id"]): j for j in previous["jobs"]}
    callback_before = getattr(adapter, "verification_callback", None)
    timeout_before = getattr(adapter, "verification_timeout", 180)
    waiting_for_human = False

    def verification(page, endpoint, operation, status):
        nonlocal waiting_for_human
        if status == "needs_human" and not waiting_for_human:
            stats["verification_handoffs"] += 1
            waiting_for_human = True
        elif status == "cleared":
            stats["verification_cleared"] += 1
            waiting_for_human = False
        stats["listing_page_reused"] = getattr(adapter, "listing_page_reused", False)
        evidence = record_verification(page, directory, source, endpoint, operation, status, acquisition=stats)
        if on_verification:
            on_verification(status, evidence, dict(stats))
        if progress:
            progress({"phase": "verification", "status": status, "evidence": evidence})
        return evidence

    with ExitStack() as stack:
        opened = False

        def ensure_session():
            nonlocal opened
            if not opened and hasattr(adapter, "session"):
                stack.enter_context(adapter.session())
                opened = True

        if hasattr(adapter, "verification_callback"):
            adapter.verification_callback = verification
            adapter.verification_timeout = verification_wait
        try:
            listing_path = directory / "listing.json"
            if listing_path.exists():
                listing = load_cache(listing_path, source)
                if listing.get("split_details") != split or not listing["complete"]:
                    raise ValueError("断点与当前列表协议不一致，请启动新一轮抓取")
            else:
                pages = directory / "pages"
                cached_pages = list(pages.glob("*.json"))
                # A long interruption can move jobs between upstream pages. Keep
                # old evidence, but refresh an expired HTTP prefix before merging.
                if (cached_pages and not getattr(adapter, "requires_listing_replay", False)
                        and time.time() - min(p.stat().st_mtime for p in cached_pages) > 900):
                    pages.rename(directory / ("expired-pages-" + uuid.uuid4().hex[:8]))
                    stats["listing_restarts"] += 1
                def fetch(page):
                    path = directory / "pages" / f"{page:04d}.json"
                    replay = path.exists() and getattr(adapter, "requires_listing_replay", False)
                    if path.exists() and not replay:
                        cached = json.loads(path.read_text(encoding="utf-8"))
                        if cached["source_id"] != source or cached["page"] != page or cached["split_details"] != split:
                            raise ValueError("分页断点与当前数据源不一致")
                        stats["listing_cache_pages"] += 1
                        return JobPage(page, cached["total"], cached["jobs"], cached["has_more"])
                    ensure_session()
                    setup_before = getattr(adapter, "listing_setup_requests", 0)
                    try:
                        result = adapter.fetch_list_page(page) if split else adapter.fetch_page(page)
                    finally:
                        stats["listing_setup_requests"] += getattr(adapter, "listing_setup_requests", 0) - setup_before
                        stats["listing_page_reused"] = getattr(adapter, "listing_page_reused", False)
                    stats["listing_network_pages"] += 1
                    stats["listing_replayed_pages"] += int(replay)
                    save_cache({"source_id": source, "page": result.page, "total": result.total,
                                "jobs": result.jobs, "has_more": result.has_more, "split_details": split}, path)
                    return result

                try:
                    listing = crawl_fn(source=source, fetch=fetch, max_pages=0, delay=delay,
                        progress=(lambda p: progress({**p, "phase": "list"})) if progress else None)
                except PaginationError as exc:
                    invalid = directory / "pages" / f"{exc.page:04d}.json"
                    if invalid.exists():
                        invalid.rename(directory / (f"invalid-page-{exc.page}-" + uuid.uuid4().hex[:8] + ".json"))
                    raise
                if not listing["complete"]:
                    raise RuntimeError("列表抓取未完成，保留旧缓存和历史")
                listing["split_details"] = split
                save_cache(listing, listing_path)

            jobs, updated = [], []
            for index, summary in enumerate(listing["jobs"]):
                job_id = str(summary["id"])
                if summary["source"] != source or not job_id:
                    raise ValueError("列表职位身份无效")
                old = previous_jobs.get(job_id)
                if split:
                    signature = digest({k: summary.get(k) for k in LIST_FIELDS})
                    path = details / (digest(job_id) + ".json")
                    entry = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
                    if entry and (entry.get("source_id") != source or str(entry["job"]["id"]) != job_id or entry["job"]["source"] != source):
                        raise ValueError("详情缓存身份不一致")
                    cached_job = entry.get("listing_facts", entry["job"]) if entry else old or {}
                    facts_changed = any(summary.get(k + "_raw") and summary[k + "_raw"] != cached_job.get(k + "_raw")
                                        for k in ("salary", "education", "office_mode", "employment_type"))
                    if entry and entry["listing_hash"] == signature and not facts_changed:
                        job = {**entry["job"], "details_fetched_at": entry["job"].get("details_fetched_at")}
                        stats["detail_cache_hits"] += 1
                    else:
                        # Bootstrap older full snapshots without re-fetching unchanged
                        # details. Only compare fields actually present in the list.
                        fields = [k for k in LIST_FIELDS if summary.get(k)]
                        unchanged_legacy = old and not old.get("details_pending") and digest({k: old.get(k) for k in fields}) == digest({k: summary.get(k) for k in fields})
                        if not entry and unchanged_legacy and not facts_changed:
                            job = {**old, "details_fetched_at": old.get("details_fetched_at")}
                            stats["legacy_cache_hits"] += 1
                        elif defer_details:
                            cached = entry["job"] if entry else old
                            job = {**(cached or summary), **{k: summary[k] for k in LIST_FIELDS if k in summary},
                                   **{k + "_raw": summary[k + "_raw"] for k in ("salary", "education", "office_mode", "employment_type") if summary.get(k + "_raw")},
                                   "details_pending": True}
                            stats["details_deferred"] += 1
                        else:
                            ensure_session()
                            if stats["detail_requests"]:
                                time.sleep(delay)
                            stats["detail_requests"] += 1
                            job = adapter.fetch_job_detail(summary)
                            # Detail endpoints may omit facts published only in the list.
                            for key in ("salary", "education", "office_mode", "employment_type"):
                                if summary.get(key + "_raw") and not job.get(key + "_raw"):
                                    job[key + "_raw"] = summary[key + "_raw"]
                            job["details_fetched_at"] = datetime.now(timezone.utc).isoformat()
                        if job["source"] != source or str(job["id"]) != job_id:
                            raise ValueError("详情 ID 与列表不一致")
                        if not job.get("details_pending"):
                            save_cache({"source_id": source, "listing_hash": signature, "job": job,
                                        "listing_facts": {k + "_raw": summary.get(k + "_raw") for k in ("salary", "education", "office_mode", "employment_type")}}, path)
                    if old is None and entry:
                        old = entry["job"]
                else:
                    job = summary
                    stats["embedded_details"] += 1
                job = {**job, **normalize_fields(job), "recruitment_type": job_recruitment_type(job, adapter.url)}
                if old and content_hash(old, adapter.url) != content_hash(job, adapter.url):
                    updated.append(job_id)
                jobs.append(job)
                if progress:
                    progress({"phase": "details", "completed": index + 1, "total": len(listing["jobs"]), **stats})
            pending = sum(bool(job.get("details_pending")) for job in jobs)
            result = {**listing, "jobs": jobs, "listing_fetched_at": listing["fetched_at"],
                      "fetched_at": datetime.now(timezone.utc).isoformat(),
                      "details_complete": not pending,
                      "pending_detail_count": pending}
            result.pop("split_details", None)
            return result, stats, updated
        except Exception as exc:
            exc.acquisition = stats
            raise
        finally:
            if hasattr(adapter, "verification_callback"):
                adapter.verification_callback = callback_before
                adapter.verification_timeout = timeout_before
