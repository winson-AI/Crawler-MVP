import argparse
import json
import sys
from pathlib import Path

from .crawler import DEFAULT_CACHE, cache_path, crawl, export_csv, load_cache, save_cache
from .rankers import DEFAULT_RANKER, RANKERS, rank_jobs
from .selectors import select_jobs
from .server import make_server
from .sources import list_sources
from .sync import DEFAULT_OUTPUT, DEFAULT_STATE, sync_jobs


def main():
    parser = argparse.ArgumentParser(description="可扩展职位爬虫、条件 selector 与 ranker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sources", help="查看可用数据源")
    sync_parser = sub.add_parser("sync", help="全量刷新并导出跨次去重的新增职位")
    sync_parser.add_argument("--sources", nargs="+", choices=[s["id"] for s in list_sources()])
    sync_parser.add_argument("--initialize", action="store_true", help="仅用现有缓存建立去重基线，不抓取")
    sync_parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sync_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    sync_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    sync_parser.add_argument("--selector", type=Path)
    sync_parser.add_argument("--ranker", choices=RANKERS, default=DEFAULT_RANKER)
    sync_parser.add_argument("--delay", type=float, default=0.4)
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
        if args.command == "sync":
            selector = json.loads(args.selector.read_text(encoding="utf-8")) if args.selector else None
            report = sync_jobs(sources=args.sources, state_path=args.state, cache=args.cache,
                output=args.output, initialize=args.initialize, selector=selector,
                ranker=args.ranker, delay=args.delay, progress=lambda s, p: print(
                    f"{s} 第 {p['page']} 页 · {p['collected']}/{p['reported_total']}", file=sys.stderr)
                if p["page"] == 1 or p["page"] % 20 == 0 else None)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report.get("failed_sources"):
                return 1
        elif args.command == "crawl":
            snapshot = crawl(args.max_pages, args.delay, lambda p: print(
                f"第 {p['page']} 页 · {p['collected']}/{p['reported_total']}", file=sys.stderr), source=args.source)
            save_cache(snapshot, cache_path(args.source, args.cache))
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
