"""Product acceptance: real snapshots, intention editing, source health, and optional live Baidu."""
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
from urllib.error import HTTPError

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
    parser.add_argument("--health-report", type=Path, help="回放已有真实同步报告，仅将缓存路径映射到验收目录")
    parser.add_argument("--output", type=Path, default=Path("output") / ("product-live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
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
        # These checks use actual existing jobs, never manufactured postings.
        home = request("/api/home")
        assert home["followup"]["planned"] == 1
        for item in home["intents"]:
            current = request("/api/inbox/export", {"intent_id": item["id"], "state": "all", "current": 1})
            assert item["matching"] == current["total"]
            assert item["unread"] == sum(j["read_state"] == "unread" for j in current["jobs"])
        original = next(i for i in request("/api/intents")["intents"] if i["id"] == intents[1]["id"])
        q = {"intent_id": original["id"], "state": "all"}
        before_edit = request("/api/inbox/export", q)
        before_current = request("/api/inbox/export", {**q, "current": 1})
        identity = lambda j: (j["source"], j["id"])
        old_states = {identity(j): (j["inbox_version"], j["read_state"]) for j in before_edit["jobs"]}
        change = {**original, "selector": {}}
        preview = request("/api/intents/preview", body=change)
        assert request("/api/inbox/export", q)["jobs"] == before_edit["jobs"]
        saved = request("/api/intents", body={**change, "expected_revision": preview["expected_revision"]})
        expanded = request("/api/inbox/export", q)
        assert all((j["inbox_version"], j["read_state"]) == old_states[identity(j)]
                   for j in expanded["jobs"] if identity(j) in old_states)
        added = [j for j in expanded["jobs"] if identity(j) not in old_states]
        assert added and all(j["read_state"] == "read" and j["change_kind"] == "baseline" for j in added)
        try:
            request("/api/intents", body={**change, "expected_revision": preview["expected_revision"]})
            raise AssertionError("stale revision was accepted")
        except HTTPError as error:
            assert error.code == 400
        request("/api/intents/undo", body={"id": saved["id"], "expected_revision": saved["revision"]})
        restored_current = request("/api/inbox/export", {**q, "current": 1})
        assert {identity(j) for j in restored_current["jobs"]} == {identity(j) for j in before_current["jobs"]}
        followup_query = {"sources": "all", "applications": 1, "application_status": "planned"}
        assert request("/api/jobs", followup_query)["jobs"] == request("/api/export", followup_query)["jobs"]
        report["product_workflow"] = {"home_matching_counts": "passed", "edit_preview": preview,
            "existing_read_versions_preserved": len(old_states), "new_existing_jobs_baselined": len(added),
            "undo_restored_matching_scope": "passed", "stale_revision_rejected": True,
            "followup_export_consistent": "passed"}
        if args.health_report:
            evidence = json.loads(args.health_report.read_text())
            replay = {**evidence, "config": {**evidence["config"], "cache": str(cache.resolve())},
                      "acceptance_replay_source": str(args.health_report.resolve())}
            save_cache(replay, args.output / "output/incremental/health-replay/report.json")
            health = request("/api/health")
            successful = sum(r["status"] == "ok" for r in evidence["sources"].values())
            assert health["latest_run"]["successful"] == successful
            if not successful:
                assert health["latest_run"]["outcome"] == "no_fresh_data" and health["latest_run"]["new_count"] is None
            assert any(not s["schedulable"] and s["mode"] == "document" for s in health["sources"])
            assert any(not s["schedulable"] and s["mode"] == "browser" for s in health["sources"])
            report["health_replay"] = {"original": str(args.health_report.resolve()),
                "sha256": hashlib.sha256(args.health_report.read_bytes()).hexdigest(), "summary": health["latest_run"],
                "manual_source_modes_distinguished": True}
            save_cache(health, args.output / "health-replay.json")
        if args.live_baidu:
            live = sync_jobs(sources=["baidu"], cache=cache, state_path=cache.parent / "sync-state.json", output=args.output / "output/incremental")
            report["live_baidu"] = {"report": live["report_path"], "result": live["sources"]["baidu"], "personal_updates": live["personal_updates"]}
            assert not live["personal_updates"]["errors"]
            health = next(s for s in request("/api/health")["sources"] if s["source"] == "baidu")
            assert health["status"] == live["sources"]["baidu"]["status"]
            report["live_baidu"]["health_status"] = health["status"]
        assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in originals.items())
        live_failed = args.live_baidu and live["sources"]["baidu"]["status"] != "ok"
        report.update(status="passed_with_live_source_failure" if live_failed else "passed", production_caches_and_history_unchanged=True)
        save_cache(report, args.output / "validation.json")
        print(json.dumps({"status": report["status"], "intents": report["historical_changes"], "report": str((args.output / "validation.json").resolve())}, ensure_ascii=False))
        if live_failed:
            raise SystemExit(1)
    finally:
        stop()


if __name__ == "__main__":
    main()
