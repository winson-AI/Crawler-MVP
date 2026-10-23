import argparse
import json
import sys
from pathlib import Path

from .crawler import DEFAULT_CACHE, cache_path, crawl, export_csv, load_cache, save_cache, stamp_snapshot
from .rankers import DEFAULT_RANKER, RANKERS, rank_jobs
from .selectors import select_jobs
from .server import make_server
from .sources import list_sources
from .sync import DEFAULT_OUTPUT, DEFAULT_STATE, sync_jobs
from .verification import ai_visual_review, record_visual_review


def sync_progress(source, progress):
    phase = progress["phase"]
    if phase == "verification":
        action = "验证已通过，继续抓取" if progress["status"] == "cleared" else "请在抓取程序的原窗口手动完成验证"
        print(f"{source} Browser Agent：{progress['status']} · 验证证据 {progress['evidence']}；{action}", file=sys.stderr, flush=True)
    elif phase == "list" and (progress["page"] == 1 or progress["page"] % 20 == 0):
        print(f"{source} 列表第 {progress['page']} 页 · {progress['collected']}/{progress['reported_total']}", file=sys.stderr, flush=True)
    elif phase == "details" and (progress["completed"] == 1 or progress["completed"] % 100 == 0 or progress["completed"] == progress["total"]):
        print(f"{source} 详情处理 {progress['completed']}/{progress['total']} · 请求 {progress['detail_requests']} · 缓存 {progress['detail_cache_hits'] + progress['legacy_cache_hits']}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description="可扩展职位爬虫、条件 selector 与 ranker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sources", help="查看可用数据源")
    review_parser = sub.add_parser("verification-review", help="记录 Codex 对验证截图的页面状态分类，不解题或代填")
    review_parser.add_argument("--evidence", type=Path, required=True)
    review_parser.add_argument("--state", choices=["ready", "needs_human", "unknown"], required=True)
    ai_review_parser = sub.add_parser("verification-ai-review", help="用视觉模型分类验证弹窗，不解题或代填")
    ai_review_parser.add_argument("--evidence", type=Path, required=True)
    ai_review_parser.add_argument("--model")
    sync_parser = sub.add_parser("sync", help="先对比列表，只补抓新增或更新的详情，支持断点恢复")
    sync_parser.add_argument("--sources", nargs="+", choices=[s["id"] for s in list_sources()])
    sync_parser.add_argument("--initialize", action="store_true", help="仅用现有缓存建立去重基线，不抓取")
    sync_parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sync_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    sync_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    sync_parser.add_argument("--selector", type=Path)
    sync_parser.add_argument("--ranker", choices=RANKERS, default=DEFAULT_RANKER)
    sync_parser.add_argument("--delay", type=float, help="请求间隔秒数；新运行默认 0.4，恢复时默认沿用原值，可显式覆盖")
    sync_parser.add_argument("--resume", type=Path, help="恢复某次运行目录中的未完成来源")
    sync_parser.add_argument("--verification-wait", type=float, default=600, help="等待人工验证的秒数（默认 600）；超时后保存暂停状态")
    sync_parser.add_argument("--pdd-details", choices=["defer", "full"], help="PDD 详情：新运行默认 defer，只更新列表并复用详情缓存；full 补抓待处理详情。恢复默认沿用原模式")
    crawl_parser = sub.add_parser("crawl", help="抓取公开职位并缓存")
    crawl_parser.add_argument("--max-pages", type=int, default=0, help="0=全量")
    crawl_parser.add_argument("--delay", type=float, default=0.4)
    crawl_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    crawl_parser.add_argument("--source", default="baidu", choices=[s["id"] for s in list_sources()])
    filter_parser = sub.add_parser("filter", help="筛选缓存，导出 JSON 或 CSV")
    filter_parser.add_argument("--selector", type=Path, required=True)
    filter_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    filter_parser.add_argument("--format", choices=["json", "csv"], default="json")
    filter_parser.add_argument("--output", type=Path)
    filter_parser.add_argument("--source", default="baidu", choices=[s["id"] for s in list_sources()])
    filter_parser.add_argument("--ranker", choices=RANKERS, default=DEFAULT_RANKER)
    serve_parser = sub.add_parser("serve", help="启动本地筛选页面")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args()
    try:
        if args.command == "verification-review":
            print(json.dumps(record_visual_review(args.evidence, args.state), ensure_ascii=False, indent=2))
        elif args.command == "verification-ai-review":
            print(json.dumps(ai_visual_review(args.evidence, model=args.model), ensure_ascii=False, indent=2))
        elif args.command == "sync":
            selector = json.loads(args.selector.read_text(encoding="utf-8")) if args.selector else None
            report = sync_jobs(sources=args.sources, state_path=args.state, cache=args.cache,
                output=args.output, initialize=args.initialize, selector=selector,
                ranker=args.ranker, delay=args.delay, resume=args.resume,
                verification_wait=args.verification_wait, progress=sync_progress,
                pdd_details=args.pdd_details)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report.get("failed_sources") or report.get("personal_updates", {}).get("errors"):
                return 1
            if report.get("paused_sources"):
                return 2
        elif args.command == "crawl":
            previous = load_cache(cache_path(args.source, args.cache), args.source)
            snapshot = crawl(args.max_pages, args.delay, lambda p: print(
                f"第 {p['page']} 页 · {p['collected']}/{p['reported_total']}", file=sys.stderr), source=args.source)
            save_cache(stamp_snapshot(snapshot, previous, ledger=Path(args.cache).parent / "observations.sqlite3"),
                       cache_path(args.source, args.cache))
            from .inbox import process_inbox
            snapshot["personal_updates"] = process_inbox(cache=args.cache)
            print(json.dumps({k: v for k, v in snapshot.items() if k != "jobs"}, ensure_ascii=False, indent=2))
        elif args.command == "filter":
            path = cache_path(args.source, args.cache)
            if not path.exists():
                raise ValueError("缓存不存在，请先执行 crawl")
            selector = json.loads(args.selector.read_text(encoding="utf-8"))
            snapshot = load_cache(path, args.source)
            jobs = rank_jobs(select_jobs(snapshot["jobs"], selector), args.ranker)
            output = export_csv(jobs) if args.format == "csv" else json.dumps(
                {"selector": selector, "ranker": args.ranker, "snapshot": {k: v for k, v in snapshot.items() if k != "jobs"}, "jobs": jobs},
                ensure_ascii=False, indent=2)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output, encoding="utf-8")
            else:
                print(output)
            print(f"匹配 {len(jobs)}/{len(snapshot['jobs'])} 条 · 缓存全量={snapshot['complete']}", file=sys.stderr)
        elif args.command == "sources":
            print(json.dumps(list_sources(), ensure_ascii=False, indent=2))
        else:
            server = make_server(args.host, args.port, args.cache)
            print(f"打开 http://{args.host}:{args.port}", flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"错误: {exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
