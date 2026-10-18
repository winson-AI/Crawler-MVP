import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .crawler import DEFAULT_CACHE, cache_path, crawl, export_csv, load_cache, save_cache
from .rankers import DEFAULT_RANKER, rank_jobs
from .selectors import select_jobs
from .sources import get_source, list_sources


def make_server(host="127.0.0.1", port=8000, cache=DEFAULT_CACHE):
    state = {"running": False, "error": None, "progress": None, "source_id": None, "source_name": None}
    lock = threading.Lock()

    def refresh(max_pages, source):
        try:
            def progress(value):
                with lock:
                    state["progress"] = value
            save_cache(crawl(max_pages=max_pages, progress=progress, source=source), cache_path(source, cache))
        except Exception as exc:
            with lock:
                state["error"] = str(exc)
        finally:
            with lock:
                state["running"] = False

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, value, content_type="application/json; charset=utf-8", filename=None):
            body = (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            try:
                query = parse_qs(url.query)
                source = query.get("source", ["baidu"])[0]
                if url.path == "/":
                    return self.send(200, (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
                if url.path == "/api/sources":
                    return self.send(200, {"sources": list_sources()})
                if url.path == "/api/status":
                    snapshot = load_cache(cache_path(source, cache), source)
                    with lock:
                        status = dict(state)
                    return self.send(200, {**status, "snapshot": {k: v for k, v in snapshot.items() if k != "jobs"}})
                if url.path in ("/api/jobs", "/api/export"):
                    selector = json.loads(query.get("selector", ["{}"])[0])
                    ranker = query.get("ranker", [DEFAULT_RANKER])[0]
                    snapshot = load_cache(cache_path(source, cache), source)
                    jobs = rank_jobs(select_jobs(snapshot["jobs"], selector), ranker)
                    if url.path == "/api/export":
                        fmt = query.get("format", ["json"])[0]
                        if fmt not in ("csv", "json"):
                            raise ValueError("format 必须为 json 或 csv")
                        if fmt == "csv":
                            return self.send(200, export_csv(jobs), "text/csv; charset=utf-8", "jobs.csv")
                        return self.send(200, {"selector": selector, "ranker": ranker, "snapshot": {k: v for k, v in snapshot.items() if k != "jobs"}, "jobs": jobs}, filename="jobs.json")
                    return self.send(200, {"total": len(jobs), "source": source, "ranker": ranker, "jobs": jobs})
                self.send(404, {"error": "路径不存在"})
            except ValueError as exc:
                self.send(400, {"error": str(exc)})
            except OSError as exc:
                self.send(500, {"error": str(exc)})

        def do_POST(self):
            if self.path != "/api/crawl":
                return self.send(404, {"error": "路径不存在"})
            # Only the local same-origin UI can start a crawl through the browser.
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                return self.send(403, {"error": "不允许跨站请求"})
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 4096:
                    raise ValueError("请求体必须为 1–4096 字节")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("请求体必须为 JSON 对象")
                max_pages = body.get("max_pages", 0)
                source = body.get("source", "baidu")
                adapter = get_source(source)
                if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 0 <= max_pages <= 1000:
                    raise ValueError("max_pages 必须为 0–1000 的整数")
                with lock:
                    if state["running"]:
                        return self.send(409, {"error": "抓取正在进行"})
                    state.update(running=True, error=None, progress=None, source_id=source, source_name=adapter.name)
                threading.Thread(target=refresh, args=(max_pages, source), daemon=True).start()
                self.send(202, {"running": True})
            except ValueError as exc:
                self.send(400, {"error": str(exc)})

    return ThreadingHTTPServer((host, port), Handler)
