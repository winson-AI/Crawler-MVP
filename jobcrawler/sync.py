"""List-first refreshes with persistent, source-scoped discovery history."""
import fcntl
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .crawler import DEFAULT_CACHE, cache_path, crawl, export_csv, load_cache, save_cache, stamp_snapshot
from .rankers import DEFAULT_RANKER, publication_time, rank_jobs
from .selectors import select_jobs, validate_selector
from .sources import get_source, list_sources
from .incremental import collect_incremental
from .verification import VerificationRequired

DEFAULT_STATE = DEFAULT_CACHE.parent / "sync-state.json"
DEFAULT_OUTPUT = DEFAULT_CACHE.parent.parent / "output" / "incremental"


def sync_jobs(sources=None, state_path=DEFAULT_STATE, cache=DEFAULT_CACHE,
              output=DEFAULT_OUTPUT, initialize=False, selector=None,
              ranker=DEFAULT_RANKER, delay=None, progress=None, resume=None,
              verification_wait=600, pdd_details=None):
    report = None
    if resume:
        if initialize or selector is not None:
            raise ValueError("恢复时不能重新初始化或更改 selector")
        run_path = Path(resume).resolve()
        report = json.loads((run_path / "report.json").read_text(encoding="utf-8"))
        if report.get("version") != 2:
            raise ValueError("该运行不支持新版断点恢复，请启动新一轮抓取")
        config = report["config"]
        state_path, cache = Path(config["state"]), Path(config["cache"])
        if sources and not set(sources) <= set(config["sources"]):
            raise ValueError("恢复来源不属于原运行")
        sources = sources or config["sources"]
        selector, ranker = report["selector"], report["ranker"]
        delay = config["delay"] if delay is None else delay
        pdd_details = config.get("pdd_details", "full") if pdd_details is None else pdd_details
    delay = 0.4 if delay is None else delay
    pdd_details = "defer" if pdd_details is None else pdd_details
    if pdd_details not in ("defer", "full"):
        raise ValueError("PDD 详情模式必须为 defer 或 full")
    if not 0.1 <= delay <= 60 or not 0 <= verification_wait <= 1800:
        raise ValueError("抓取间隔应为 0.1–60 秒，验证等待应为 0–1800 秒")
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

        if report is None:
            started = datetime.now(timezone.utc)
            run_path = Path(output).resolve() / (started.strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
            run_path.mkdir(parents=True, exist_ok=False)
            report = {"version": 2, "started_at": started.isoformat(), "seeded": seeded,
                      "selector": selector, "ranker": ranker, "sources": {},
                      "config": {"state": str(state_path.resolve()), "cache": str(Path(cache).resolve()),
                                 "sources": sources, "delay": delay, "pdd_details": pdd_details},
                      "report_path": str(run_path / "report.json")}
        else:
            report["resumed_at"] = datetime.now(timezone.utc).isoformat()
            report["config"]["delay"] = delay
            report["config"]["pdd_details"] = pdd_details
            report.pop("finished_at", None)
        save_cache(report, run_path / "report.json")
        for source in sources:
            prior = report["sources"].get(source, {})
            filling_pending = source == "pinduoduo" and pdd_details == "full" and prior.get("pending_detail_count", 0)
            if prior.get("status") == "ok" and not filling_pending:
                continue
            old = state["sources"][source]
            seen = set(old["seen_ids"])
            checkpoint = run_path / "checkpoints" / source

            def verification_status(status, evidence, acquisition, source=source):
                report["sources"][source] = {"status": "running" if status == "cleared" else "waiting_for_human",
                    "verification": evidence, "resume": str(run_path), "checkpoint": str(checkpoint),
                    "acquisition": acquisition}
                save_cache(report, run_path / "report.json")

            try:
                previous = load_cache(cache_path(source, cache), source)
                if (resume and (publication_time(previous.get("fetched_at")) or 0) > publication_time(report["started_at"])
                        and previous.get("sync_run") != str(run_path)):
                    raise ValueError("已有更新快照，不能用旧断点回退缓存；请启动新一轮抓取")
                history_before = checkpoint / "history-before.json"
                if history_before.exists():
                    baseline = json.loads(history_before.read_text(encoding="utf-8"))
                    if baseline.get("source_id") != source or not isinstance(baseline.get("seen_ids"), list):
                        raise ValueError("断点历史与当前数据源不一致")
                    seen = set(baseline["seen_ids"])
                else:
                    save_cache({"source_id": source, "seen_ids": sorted(seen)}, history_before)
                snapshot, acquisition, changed = collect_incremental(source,
                    previous, checkpoint, state_path.parent / "details", crawl,
                    delay=delay, progress=(lambda p, s=source: progress(s, p)) if progress else None,
                    on_verification=verification_status, verification_wait=verification_wait,
                    defer_details=source == "pinduoduo" and pdd_details == "defer")
                if not snapshot["complete"]:
                    raise RuntimeError("抓取未完成，保留旧缓存和历史")
                snapshot = stamp_snapshot(snapshot, load_cache(checkpoint / "before.json", source),
                    ledger=state_path.parent / "observations.sqlite3", known_ids=seen)
                discovered = {}
                for job in snapshot["jobs"]:
                    if job["source"] != source or not str(job["id"]):
                        raise ValueError("职位身份无效，保留旧缓存和历史")
                    if str(job["id"]) not in seen:
                        discovered[str(job["id"])] = job
                jobs = rank_jobs(select_jobs(list(discovered.values()), selector), ranker)
                changed = set(changed)
                updated_jobs = rank_jobs(select_jobs([j for j in snapshot["jobs"] if j["id"] in changed and j["id"] in seen], selector), ranker)
                result = {"status": "ok", "new_count": len(discovered), "exported_count": len(jobs),
                          "updated_count": sum(j in seen for j in changed), "updated_exported_count": len(updated_jobs),
                          "acquisition": acquisition, "checkpoint": str(checkpoint),
                          "details_complete": snapshot.get("details_complete", True),
                          "pending_detail_count": snapshot.get("pending_detail_count", 0),
                          "collected": snapshot["collected"], "fetched_at": snapshot["fetched_at"],
                          "json": str(run_path / f"{source}-new.json"),
                          "csv": str(run_path / f"{source}-new.csv"),
                          "updated_json": str(run_path / f"{source}-updated.json"),
                          "updated_csv": str(run_path / f"{source}-updated.csv")}
                # Write exports before advancing history. On a disk/write failure
                # the jobs remain unseen, so a retry cannot silently lose them.
                save_cache({**result, "source": source, "selector": selector,
                            "ranker": ranker, "jobs": jobs}, result["json"])
                Path(result["csv"]).write_text(export_csv(jobs), encoding="utf-8")
                save_cache({"source": source, "selector": selector, "ranker": ranker, "jobs": updated_jobs}, result["updated_json"])
                Path(result["updated_csv"]).write_text(export_csv(updated_jobs), encoding="utf-8")
                snapshot["sync_run"] = str(run_path)
                save_cache(snapshot, cache_path(source, cache))
                next_history = {"seen_ids": sorted(set(old["seen_ids"]) | set(discovered)),
                           "last_success": snapshot["fetched_at"],
                           "last_export": result["json"]}
                state["sources"][source] = next_history
                try:
                    save_cache(state, state_path)
                except Exception:
                    state["sources"][source] = old
                    raise
                report["sources"][source] = result
            except VerificationRequired as exc:
                report["sources"][source] = {"status": "paused", "error": str(exc),
                    "verification": exc.evidence, "checkpoint": str(checkpoint),
                    "acquisition": getattr(exc, "acquisition", {}), "resume": str(run_path)}
            except Exception as exc:
                report["sources"][source] = {"status": "error", "error": str(exc),
                    "checkpoint": str(checkpoint), "acquisition": getattr(exc, "acquisition", {})}
            save_cache(report, run_path / "report.json")
        report.update(finished_at=datetime.now(timezone.utc).isoformat(),
                      new_count=sum(r.get("new_count", 0) for r in report["sources"].values()),
                      updated_count=sum(r.get("updated_count", 0) for r in report["sources"].values()),
                      paused_sources=[s for s, r in report["sources"].items() if r["status"] == "paused"],
                      failed_sources=[s for s, r in report["sources"].items() if r["status"] == "error"])
        from .inbox import process_inbox
        try:
            report["personal_updates"] = process_inbox(cache=cache,
                excluded_sources=report["failed_sources"] + report["paused_sources"])
        except Exception as exc:
            report["personal_updates"] = {"errors": [{"error": str(exc)}]}
        save_cache(report, run_path / "report.json")
        return report
