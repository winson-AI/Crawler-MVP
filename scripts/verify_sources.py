"""Verify real crawls or cached snapshots, shared filters/rankers and JSON/CSV exports."""
import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobcrawler.crawler import cache_path, crawl, export_csv, load_cache, save_cache
from jobcrawler.rankers import RANKERS, publication_time, rank_jobs
from jobcrawler.selectors import select_jobs
from jobcrawler.sources import SOURCES
from jobcrawler.sources.base import JobPage


def crawl_with_checkpoints(source, max_pages, directory):
    """Keep successful pages from this run so transient failures need no full restart."""
    adapter = SOURCES[source]
    directory = directory / source
    directory.mkdir(parents=True, exist_ok=True)

    def fetch(page):
        path = directory / f"{page:04d}.json"
        # Browser adapters must replay navigation to establish their normal session state.
        if path.exists() and not hasattr(adapter, "session"):
            cached = json.loads(path.read_text())
            if cached["source"] != source or cached["page"] != page:
                raise RuntimeError("检查点数据源或页码不一致")
            return JobPage(page, cached["total"], cached["jobs"], cached.get("has_more"))
        result = adapter.fetch_page(page)
        save_cache({"source": source, "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "page": result.page, "total": result.total, "jobs": result.jobs,
                    "has_more": result.has_more}, path)
        return result

    session = adapter.session() if hasattr(adapter, "session") else nullcontext()
    with session:
        snapshot = crawl(source=source, max_pages=max_pages, fetch=fetch,
                         progress=lambda progress: print(f"{source}: page {progress['page']} · {progress['collected']}/{progress['reported_total']}", flush=True))
    snapshot["checkpoint_directory"] = str(directory.resolve())
    return snapshot


def verify_pipeline(snapshot, output):
    jobs = snapshot["jobs"]
    if not jobs:
        raise RuntimeError("真实列表为空")
    if len({job["id"] for job in jobs}) != len(jobs):
        raise RuntimeError("快照包含重复职位 ID")
    if any(job["source"] != snapshot["source_id"] for job in jobs):
        raise RuntimeError("快照混入其他数据源")
    sample = next((job for job in jobs if job["technologies"] and job["locations"]), None)
    if sample is None:
        sample = next((job for job in jobs if job["locations"]), None)
    if sample is None:
        raise RuntimeError("样本中没有可验证地点的职位")
    years = sample["experience"]["min"]
    selector = {"locations": sample["locations"][:1], "years": years if years is not None else 10,
                "include_unknown_experience": True, "technologies": sample["technologies"][:1],
                "keywords": [sample["title"]]}
    selected = select_jobs(jobs, selector)
    if not any(job["id"] == sample["id"] for job in selected):
        raise RuntimeError("组合 selector 未匹配真实样本")
    if select_jobs([sample], {**selector, "exclude_keywords": [sample["title"]]}):
        raise RuntimeError("排除关键词没有排除真实样本")
    if select_jobs(jobs, {"technologies": ["__crawler_nonexistent_technology_7bc2__"]}):
        raise RuntimeError("不存在的技术词产生了匹配")
    # Verify year boundaries independently of the text extraction itself.
    for years in (0, 3, 5, 10):
        actual = {job["id"] for job in select_jobs(jobs, {"years": years})}
        expected = {job["id"] for job in jobs if job["experience"]["min"] is not None
                    and years >= job["experience"]["min"]
                    and (job["experience"]["max"] is None or years <= job["experience"]["max"])}
        if actual != expected:
            raise RuntimeError(f"{years} 年 selector 与结构化年限边界不一致")
    for ranker in RANKERS:
        ranked = rank_jobs(jobs, ranker)
        values = [publication_time(job["published_at"]) if ranker.startswith("published_") or ranker == "relevance_desc" else job["experience"]["min"] for job in ranked]
        known = [value for value in values if value is not None]
        if values != known + [None] * (len(values) - len(known)):
            raise RuntimeError("ranker 没有将未知值放到最后")
        if known != sorted(known, reverse=ranker.endswith("_desc")):
            raise RuntimeError("ranker 顺序错误")
        text = export_csv(ranked)
        rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
        def csv_text(value):
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        if [row["title"] for row in rows] != [csv_text(job["title"]) for job in ranked]:
            raise RuntimeError("CSV 与排序结果不一致")
        for row, job in zip(rows, ranked):
            for field in ("source", "company", "requirements", "description", "published_at", "updated_at", "url"):
                if row[field] != csv_text(job[field]):
                    raise RuntimeError(f"CSV 字段未完整保留: {field}")
    ranked = rank_jobs(selected, "experience_desc")
    exported = {"selector": selector, "ranker": "experience_desc", "snapshot": {k: v for k, v in snapshot.items() if k != "jobs"}, "jobs": ranked}
    source = snapshot["source_id"]
    (output / f"{source}-matches.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2))
    (output / f"{source}-matches.csv").write_text(export_csv(ranked))
    all_exported = {"selector": {}, "ranker": "published_desc", "snapshot": exported["snapshot"],
                    "jobs": rank_jobs(jobs)}
    json_text = json.dumps(all_exported, ensure_ascii=False, indent=2)
    if json.loads(json_text) != all_exported:
        raise RuntimeError("JSON 导出往返不一致")
    (output / f"{source}-all.json").write_text(json_text)
    (output / f"{source}-all.csv").write_text(export_csv(all_exported["jobs"]))
    return {"selector": selector, "matched": len(selected), "rankers_verified": list(RANKERS),
            "technology_sample_available": bool(sample["technologies"]),
            "requirements_present": sum(bool(job["requirements"]) for job in jobs),
            "missing_requirements": sum(not job["requirements"] for job in jobs),
            "missing_description": sum(not job["description"] for job in jobs),
            "year_boundaries_verified": [0, 3, 5, 10],
            "all_exported": len(jobs),
            "known_experience": sum(job["experience"]["min"] is not None for job in jobs),
            "known_publication": sum(bool(job["published_at"]) for job in jobs),
            "sample": {key: sample[key] for key in ("id", "title", "url", "experience", "technologies")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", nargs="+", choices=list(SOURCES), default=[s for s in SOURCES if s != "baidu"])
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--reuse-cache", action="store_true", help="使用已有真实快照；无缓存仍进行抓取")
    parser.add_argument("--require-complete", action="store_true", help="只接受已完成全部分页的快照")
    parser.add_argument("--checkpoint-dir", type=Path, help="保存并复用本次抓取的成功分页；每次新抓取使用新目录")
    parser.add_argument("--output", type=Path, default=Path("output/verification"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "max_pages": args.max_pages, "sources": []}
    failed = False
    for source in args.sources:
        path = cache_path(source)
        original = path.read_bytes() if path.exists() else None
        result = {"source": source, "url": SOURCES[source].url}
        snapshot = None
        try:
            cached = args.reuse_cache and path.exists()
            if cached:
                snapshot = load_cache(path, source)
            elif args.checkpoint_dir:
                snapshot = crawl_with_checkpoints(source, args.max_pages, args.checkpoint_dir)
            else:
                snapshot = crawl(source=source, max_pages=args.max_pages)
            if args.require_complete and not snapshot["complete"]:
                raise RuntimeError("快照为部分数据，未通过全量验证")
            result.update({key: snapshot[key] for key in ("fetched_at", "reported_total", "collected", "pages", "complete")})
            result.update({key: snapshot[key] for key in ("scanned", "duplicates") if key in snapshot})
            if "identity_check" in snapshot:
                result["identity_check"] = snapshot["identity_check"]
            result["checks"] = verify_pipeline(snapshot, args.output)
            if not cached:
                save_cache(snapshot, path)
            result.update(status="passed", acquisition="existing_live_snapshot" if cached else
                          "live_crawl_with_checkpoints" if args.checkpoint_dir else "live_crawl")
            print(f"{source}: PASS {snapshot['collected']}/{snapshot['reported_total']} · {snapshot['pages']} 页", flush=True)
        except (RuntimeError, ValueError, OSError) as exc:
            failed = True
            result.update(status="failed", error=str(exc), cache_preserved=(path.read_bytes() if path.exists() else None) == original)
            if snapshot is not None:
                failed_snapshot = args.output / f"{source}-failed-snapshot.json"
                save_cache(snapshot, failed_snapshot)
                result["failed_snapshot"] = str(failed_snapshot.resolve())
            print(f"{source}: FAIL {exc}", flush=True)
        report["sources"].append(result)
    report_path = args.output / "source-verification.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"报告: {report_path.resolve()}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
