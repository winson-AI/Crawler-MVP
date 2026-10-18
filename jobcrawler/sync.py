"""Full source refreshes with persistent, source-scoped discovery history."""
import fcntl
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .crawler import DEFAULT_CACHE, cache_path, crawl, export_csv, load_cache, save_cache
from .rankers import DEFAULT_RANKER, rank_jobs
from .selectors import select_jobs, validate_selector
from .sources import get_source, list_sources

DEFAULT_STATE = DEFAULT_CACHE.parent / "sync-state.json"
DEFAULT_OUTPUT = DEFAULT_CACHE.parent.parent / "output" / "incremental"


def sync_jobs(sources=None, state_path=DEFAULT_STATE, cache=DEFAULT_CACHE,
              output=DEFAULT_OUTPUT, initialize=False, selector=None,
              ranker=DEFAULT_RANKER, delay=0.4, progress=None):
    sources = list(dict.fromkeys(sources if sources is not None else
                                [s["id"] for s in list_sources()]))
    for source in sources:
        get_source(source)
    selector = validate_selector({} if selector is None else selector)
    rank_jobs([], ranker)  # Validate before any history/cache mutation.
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有增量抓取正在运行，本次跳过") from None
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
            "version": 1, "sources": {}}
        if state.get("version") != 1 or not isinstance(state.get("sources"), dict):
            raise ValueError("增量历史格式无效，保留原文件")
        seeded = {}
        for source in sources:
            if source not in state["sources"]:
                snapshot = load_cache(cache_path(source, cache), source)
                ids = sorted({str(j["id"]) for j in snapshot["jobs"]})
                state["sources"][source] = {"seen_ids": ids, "last_success": None}
                seeded[source] = len(ids)
            elif not isinstance(state["sources"][source].get("seen_ids"), list):
                raise ValueError(f"{source} 增量历史格式无效，保留原文件")
        # Persist the baseline before fetching: a later cache refresh must not
        # accidentally seed unseen jobs after an interrupted run.
        save_cache(state, state_path)
        if initialize:
            return {"initialized": True, "seeded": seeded, "seen": {
                s: len(state["sources"][s]["seen_ids"]) for s in sources},
                "state_path": str(state_path)}

        started = datetime.now(timezone.utc)
        run_path = Path(output) / (started.strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
        run_path.mkdir(parents=True, exist_ok=False)
        report = {"started_at": started.isoformat(), "seeded": seeded,
                  "selector": selector, "ranker": ranker, "sources": {},
                  "report_path": str(run_path / "report.json")}
        save_cache(report, run_path / "report.json")
        for source in sources:
            old = state["sources"][source]
            seen = set(old["seen_ids"])
            try:
                snapshot = crawl(max_pages=0, delay=delay, source=source,
                                 progress=(lambda p, s=source: progress(s, p)) if progress else None)
                if not snapshot["complete"]:
                    raise RuntimeError("抓取未完成，保留旧缓存和历史")
                discovered = {}
                for job in snapshot["jobs"]:
                    if job["source"] != source or not str(job["id"]):
                        raise ValueError("职位身份无效，保留旧缓存和历史")
                    if str(job["id"]) not in seen:
                        discovered[str(job["id"])] = job
                jobs = rank_jobs(select_jobs(list(discovered.values()), selector), ranker)
                result = {"status": "ok", "new_count": len(discovered), "exported_count": len(jobs),
                          "collected": snapshot["collected"], "fetched_at": snapshot["fetched_at"],
                          "json": str(run_path / f"{source}-new.json"),
                          "csv": str(run_path / f"{source}-new.csv")}
                # Write exports before advancing history. On a disk/write failure
                # the jobs remain unseen, so a retry cannot silently lose them.
                save_cache({**result, "source": source, "selector": selector,
                            "ranker": ranker, "jobs": jobs}, result["json"])
                Path(result["csv"]).write_text(export_csv(jobs), encoding="utf-8")
                save_cache(snapshot, cache_path(source, cache))
                updated = {"seen_ids": sorted(seen | set(discovered)),
                           "last_success": snapshot["fetched_at"],
                           "last_export": result["json"]}
                state["sources"][source] = updated
                try:
                    save_cache(state, state_path)
                except Exception:
                    state["sources"][source] = old
                    raise
                report["sources"][source] = result
            except Exception as exc:
                report["sources"][source] = {"status": "error", "error": str(exc)}
            save_cache(report, run_path / "report.json")
        report.update(finished_at=datetime.now(timezone.utc).isoformat(),
                      new_count=sum(r.get("new_count", 0) for r in report["sources"].values()),
                      failed_sources=[s for s, r in report["sources"].items() if r["status"] == "error"])
        save_cache(report, run_path / "report.json")
        return report
