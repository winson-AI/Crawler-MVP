import json
import threading
from contextlib import ExitStack
from queue import Queue
from threading import Event
from time import monotonic
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .crawler import DEFAULT_CACHE, cache_path, crawl, export_csv, load_cache, save_cache
from .rankers import DEFAULT_RANKER, rank_jobs
from .selectors import select_jobs
from .sources import get_source, list_sources, load_custom_sources, register_custom_source
from .sources.custom import CONFIG_PATH, MAX_DOCUMENT, CustomSource, read_configs
from .sources.templates import resolve_template, template_catalog
from .sync import sync_jobs


def make_server(host="127.0.0.1", port=8000, cache=DEFAULT_CACHE, custom_sources=None):
    custom_path = Path(custom_sources) if custom_sources is not None else CONFIG_PATH
    if custom_sources is not None:
        load_custom_sources(custom_path)
    state = {"running": False, "error": None, "progress": None, "source_id": None, "source_name": None}
    lock = threading.Lock()
    pdd_tasks = Queue()
    pdd_thread = None
    pdd_stop = Event()
    closing = False

    def refresh(max_pages, source):
        adapter = get_source(source)
        original = dict(adapter.config) if isinstance(adapter, CustomSource) else None
        try:
            def progress(value):
                with lock:
                    state["progress"] = value
            if source == "pinduoduo" and max_pages == 0:
                directory = Path(cache).parent
                report = sync_jobs(sources=[source], cache=cache, state_path=directory / "sync-state.json",
                    output=directory.parent / "output" / "incremental", pdd_details="defer", delay=6,
                    progress=lambda source, value: progress(value))
                result = report["sources"][source]
                with lock:
                    state["report_path"] = report["report_path"]
                if result["status"] != "ok":
                    raise RuntimeError(result["error"])
            else:
                snapshot = crawl(max_pages=max_pages, progress=progress, source=source)
                if original is not None and adapter.config != original:
                    register_custom_source(adapter.config, custom_path)
                save_cache(snapshot, cache_path(source, cache))
        except Exception as exc:
            if original is not None:
                adapter.__dict__.update(CustomSource(original).__dict__)
            with lock:
                state["error"] = str(exc)
        finally:
            with lock:
                state["running"] = False

    def refresh_pdd():
        # Playwright's sync objects stay on their owner thread between requests.
        next_refresh = 0
        with ExitStack() as sessions:
            while True:
                max_pages = pdd_tasks.get()
                if max_pages is None:
                    return
                try:
                    remaining = max(0, next_refresh - monotonic())
                    if remaining:
                        with lock:
                            state["progress"] = {"phase": "cooldown", "seconds": round(remaining, 1)}
                        if pdd_stop.wait(remaining):
                            return
                        with lock:
                            state["progress"] = None
                    adapter = get_source("pinduoduo")
                    if adapter._context is None or (adapter._browser is not None and not adapter._browser.is_connected()):
                        sessions.close()
                        sessions.enter_context(adapter.session())
                    refresh(max_pages, "pinduoduo")
                except Exception as exc:
                    with lock:
                        state.update(running=False, error=str(exc))
                finally:
                    # Failed refreshes also cool down instead of encouraging retries.
                    next_refresh = monotonic() + 120

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
                if url.path == "/api/templates":
                    return self.send(200, {**template_catalog(), "learned": read_configs(custom_path)})
                if url.path == "/api/status":
                    snapshot = load_cache(cache_path(source, cache), source)
                    adapter = get_source(source)
                    with lock:
                        status = dict(state)
                    return self.send(200, {**status, "configuration_changed": bool(isinstance(adapter, CustomSource)
                        and snapshot.get("fetched_at") and snapshot.get("adapter_revision") != adapter.revision),
                        "snapshot": {k: v for k, v in snapshot.items() if k != "jobs"}})
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
            nonlocal pdd_thread
            if self.path not in ("/api/crawl", "/api/sources/preview", "/api/sources/custom", "/api/templates/match"):
                return self.send(404, {"error": "路径不存在"})
            # Only the local same-origin UI can start a crawl through the browser.
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                return self.send(403, {"error": "不允许跨站请求"})
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.send(403, {"error": "不允许跨站请求"})
            if self.path != "/api/crawl":
                request_host = urlsplit("http://" + self.headers.get("Host", "")).hostname
                if request_host not in ("127.0.0.1", "localhost", "::1", host):
                    return self.send(403, {"error": "网站适配管理只允许本机或明确绑定的 Host"})
            reserved = False
            try:
                size = int(self.headers.get("Content-Length", 0))
                limit = 4096 if self.path == "/api/crawl" else MAX_DOCUMENT + 65536
                if not 0 < size <= limit:
                    raise ValueError(f"请求体必须为 1–{limit} 字节")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    raise ValueError("请求必须使用 application/json")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("请求体必须为 JSON 对象")
                if self.path == "/api/templates/match":
                    return self.send(200, resolve_template(body.get("url"), read_configs(custom_path)))
                if self.path != "/api/crawl":
                    matched = resolve_template(body.get("url"), read_configs(custom_path)) if "config" not in body else None
                    config = body.get("config") if matched is None else matched["config"]
                    if matched is not None and body.get("document") is not None:
                        config["mode"] = "document"
                    adapter = CustomSource(config)
                    committing = self.path == "/api/sources/custom"
                    if committing:
                        with lock:
                            if closing or state["running"]:
                                return self.send(409, {"error": "已有抓取或适配任务正在进行"})
                            state.update(running=True, error=None, source_id=adapter.id, source_name=adapter.name,
                                         progress={"phase": "adapting"}, report_path=None)
                            reserved = True
                    document = body.get("document")
                    if document is not None:
                        preview = adapter.parse_document(document)
                    else:
                        preview = adapter.fetch_page(1)
                    result = {"config": adapter.config, "parser_used": adapter.parser_used,
                              "template_id": adapter.config.get("template_id"), "adaptation": adapter.adaptation,
                              "network_requests": adapter.network_requests,
                              "collected": len(preview.jobs), "has_more": preview.has_more,
                              "sample": preview.jobs[:10], "scope": adapter.snapshot_metadata["scope"],
                              "acquisition": "provided_document" if document is not None else adapter.snapshot_metadata["acquisition"],
                              "warnings": (matched["warnings"] if matched else []) +
                                          (["未配置全站分页；仅表示当前 URL 或提交文档的范围"]
                                           if not adapter.config.get("pagination") else [])}
                    if not committing:
                        return self.send(200, result)
                    if adapter.config["mode"] != "document" and document is not None:
                        raise ValueError("HTTP/浏览器自动挂载必须通过实际联网预览；离线文档请使用 mode=document")
                    registered = register_custom_source(adapter.config, custom_path)
                    if adapter.config["mode"] == "document":
                        registered.parser_used = adapter.parser_used
                        registered.adaptation = adapter.adaptation
                        snapshot = crawl(source=registered.id, max_pages=1, fetch=lambda page: preview)
                        snapshot["acquisition"] = "provided_document"
                        save_cache(snapshot, cache_path(registered.id, cache))
                        with lock:
                            state.update(running=False, progress=None)
                        return self.send(200, {**result, "mounted": True, "refreshing": False})
                    threading.Thread(target=refresh, args=(0, registered.id), daemon=True).start()
                    return self.send(202, {**result, "mounted": True, "refreshing": True})
                max_pages = body.get("max_pages", 0)
                source = body.get("source", "baidu")
                adapter = get_source(source)
                if isinstance(adapter, CustomSource) and adapter.config["mode"] == "document":
                    raise ValueError("该网站使用文档导入，请在网站适配面板重新导入登录后的页面文档")
                if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 0 <= max_pages <= 1000:
                    raise ValueError("max_pages 必须为 0–1000 的整数")
                with lock:
                    if closing:
                        return self.send(503, {"error": "服务正在关闭"})
                    if state["running"]:
                        return self.send(409, {"error": "抓取正在进行"})
                    state.update(running=True, error=None, progress=None, source_id=source, source_name=adapter.name, report_path=None)
                    if source == "pinduoduo":
                        if pdd_thread is None or not pdd_thread.is_alive():
                            pdd_thread = threading.Thread(target=refresh_pdd, name="pdd-refresh", daemon=True)
                            pdd_thread.start()
                        pdd_tasks.put(max_pages)
                    else:
                        threading.Thread(target=refresh, args=(max_pages, source), daemon=True).start()
                self.send(202, {"running": True})
            except (ValueError, TypeError, RecursionError) as exc:
                if reserved:
                    with lock:
                        state.update(running=False, error=str(exc))
                self.send(400, {"error": str(exc)})
            except OSError as exc:
                if reserved:
                    with lock:
                        state.update(running=False, error=str(exc))
                self.send(500, {"error": str(exc)})

    class Server(ThreadingHTTPServer):
        def server_close(self):
            nonlocal closing
            with lock:
                closing = True
                pdd_stop.set()
                worker = pdd_thread
                if worker and worker.is_alive():
                    pdd_tasks.put(None)
            if worker:
                worker.join(timeout=5)
            from .sources.live_browser import close_browser_reader
            close_browser_reader()
            super().server_close()

    return Server((host, port), Handler)
