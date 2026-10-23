"""P1 acceptance: replay two real snapshots, then optionally sync live Baidu in isolation."""
import argparse
import csv
import hashlib
import io
import json
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobcrawler.crawler import cache_path, load_cache, save_cache
from jobcrawler.rankers import RANKERS, publication_time
from jobcrawler.server import make_server
from jobcrawler.sync import sync_jobs

SOURCES = ["baidu", "alibaba", "tencent", "meituan", "bytedance", "jd", "pinduoduo"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True, help="真实历史运行的 checkpoints 目录")
    parser.add_argument("--live-baidu", action="store_true", help="隔离执行百度全量列表增量同步")
    parser.add_argument("--output", type=Path, default=Path("output") / ("p1-live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cache = args.output / "data/jobs.json"
    cache.parent.mkdir()
    watched = list(Path("data").glob("jobs*.json")) + [Path("data/sync-state.json")]
    originals = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in watched if p.exists()}
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "sources": {},
              "scope": "Seven-source real historical replay; optional live Baidu only. Production state unchanged."}
    for source in SOURCES:
        old = args.before / source / "before.json"
        before, after = load_cache(old, source), load_cache(cache_path(source), source)
        assert before["jobs"] and after["jobs"]
        assert publication_time(before["fetched_at"]) <= publication_time(after["fetched_at"])
        shutil.copyfile(old, cache_path(source, cache))
        report["sources"][source] = {"before": str(old.resolve()), "before_at": before["fetched_at"],
                                     "after_at": after["fetched_at"], "before_count": len(before["jobs"]),
                                     "after_count": len(after["jobs"])}
    server = None

    def start():
        nonlocal server, thread
        server = make_server(port=0, cache=cache)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        thread.join()

    def request(path, query=None, body=None, raw=False):
        url = f"http://127.0.0.1:{server.server_port}" + path + ("?" + urlencode(query) if query else "")
        req = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"} if body is not None else {})
        with urlopen(req, timeout=90) as response:
            data = response.read()
            return data if raw else json.loads(data)

    thread = None
    start()
    try:
        intents = [request("/api/intents", body={"name": name, "sources": SOURCES, "selector": selector,
                     "ranker": "published_desc", "include_uncertain": True})
                   for name, selector in (("真实验收·全部岗位", {}),
                      ("真实验收·京沪 Python·3年", {"locations": ["北京", "上海"], "technologies": ["Python"], "years": 3}))]
        for intent in intents:
            assert request("/api/inbox", {"intent_id": intent["id"]})["total"] == 0
        for source in SOURCES:
            shutil.copyfile(cache_path(source), cache_path(source, cache))
        changes = request("/api/inbox/refresh", body={})
        assert not changes["errors"]
        report["historical_changes"] = changes["intents"]
        report["intents"] = {}
        for intent in intents:
            q = {"intent_id": intent["id"], "state": "unread"}
            inbox = request("/api/inbox/export", q)
            assert inbox["total"] > 0
            report["intents"][intent["name"]] = {"id": intent["id"], "counts": inbox["counts"], "rankers": {}}
            for ranker in RANKERS:
                intent = request("/api/intents", body={**intent, "ranker": ranker})
                exported = request("/api/inbox/export", q)
                a, b = request("/api/inbox", {**q, "limit": 30}), request("/api/inbox", {**q, "limit": 30, "offset": 30})
                assert a["jobs"] + b["jobs"] == exported["jobs"][:60]
                values = [publication_time(j.get("published_at")) if ranker.startswith("published") else j["experience"]["min"] for j in exported["jobs"]]
                known = [v for v in values if v is not None]
                assert values == known + [None] * (len(values) - len(known))
                assert known == sorted(known, reverse=ranker.endswith("desc"))
                data = request("/api/inbox/export", {**q, "format": "csv", "limit": 1}, raw=True)
                rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
                assert [(r["source"], r["id"], int(r["inbox_version"])) for r in rows] == [(j["source"], j["id"], j["inbox_version"]) for j in exported["jobs"]]
                stem = args.output / (intent["id"] + "-" + ranker)
                save_cache(exported, stem.with_suffix(".json"))
                stem.with_suffix(".csv").write_bytes(data)
                report["intents"][intent["name"]]["rankers"][ranker] = {"count": len(rows), "json": str(stem.with_suffix(".json").resolve()), "csv": str(stem.with_suffix(".csv").resolve())}
            request("/api/digest/settings", body={"intent_id": intent["id"], "settings": {
                "enabled": True, "frequency": "daily", "quiet_start": 0, "quiet_end": 0, "include_updates": True, "include_uncertain": True}})
        request("/api/inbox/refresh", body={})
        notifications = request("/api/notifications")["notifications"]
        assert len(notifications) == 2
        assert all(json.loads(Path(n["file"]).read_text())["entries"] == n["entries"] for n in notifications)
        request("/api/inbox/refresh", body={})
        assert len(request("/api/notifications")["notifications"]) == 2
        selected = request("/api/inbox/export", {"intent_id": intents[0]["id"]})["jobs"][0]
        read_body = {"intent_id": intents[0]["id"], "source": selected["source"], "id": selected["id"], "version": selected["inbox_version"]}
        second_before = request("/api/inbox", {"intent_id": intents[1]["id"]})["counts"]
        request("/api/inbox/read", body=read_body)
        assert request("/api/inbox", {"intent_id": intents[1]["id"]})["counts"] == second_before
        request("/api/applications", body={"source": selected["source"], "id": selected["id"], "status": "planned", "notes": "真实职位验收记录，仅本地测试，不代表实际投递"})
        request("/api/notifications/read", body={"id": notifications[0]["id"]})
        stop(); start()
        all_jobs = request("/api/inbox/export", {"intent_id": intents[0]["id"], "state": "all"})["jobs"]
        restored = next(j for j in all_jobs if (j["source"], j["id"]) == (selected["source"], selected["id"]))
        assert restored["read_state"] == "read" and restored["application_status"] == "planned"
        assert any(n["read"] for n in request("/api/notifications")["notifications"])
        assert len(request("/api/intents")["intents"]) == 2
        app = request("/api/export", {"sources": "all", "applications": 1})
        assert len(app["jobs"]) == 1
        save_cache(app, args.output / "applications.json")
        (args.output / "applications.csv").write_bytes(request("/api/export", {"sources": "all", "applications": 1, "format": "csv"}, raw=True))
        report.update(restart_persistence="passed", independent_read_state="passed", digest_files=[n["file"] for n in notifications],
                      notification_ack_separate="passed", repeated_digest_idempotent="passed")
        if args.live_baidu:
            live = sync_jobs(sources=["baidu"], cache=cache, state_path=cache.parent / "sync-state.json", output=args.output / "incremental")
            report["live_baidu"] = {"report": live["report_path"], "result": live["sources"]["baidu"], "personal_updates": live["personal_updates"]}
            assert live["sources"]["baidu"]["status"] == "ok", live["sources"]["baidu"]
            assert not live["personal_updates"]["errors"]
        assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in originals.items())
        report.update(status="passed", production_caches_and_history_unchanged=True)
        save_cache(report, args.output / "validation.json")
        print(json.dumps({"status": report["status"], "intents": report["historical_changes"], "report": str((args.output / "validation.json").resolve())}, ensure_ascii=False))
    finally:
        stop()


if __name__ == "__main__":
    main()
