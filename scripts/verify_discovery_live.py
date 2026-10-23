"""Verify P0 via HTTP using isolated copies of real snapshots and optional live Baidu page."""
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
from jobcrawler.crawler import DEFAULT_CACHE, cache_path, crawl, load_cache, save_cache, stamp_snapshot
from jobcrawler.rankers import RANKERS, publication_time
from jobcrawler.server import make_server

SOURCES = ["baidu", "alibaba", "tencent", "meituan", "bytedance", "jd", "pinduoduo"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-baidu", action="store_true", help="真实抓取百度一页，仅写入验收目录")
    parser.add_argument("--output", type=Path, default=Path("output") / ("p0-live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cache = args.output / "data/jobs.json"
    cache.parent.mkdir()
    originals, counts, legacy_keys = {}, {}, set()
    for source in SOURCES:
        path = cache_path(source)
        originals[source] = hashlib.sha256(path.read_bytes()).hexdigest()
        shutil.copyfile(path, cache_path(source, cache))
        original_jobs = load_cache(cache_path(source, cache), source)["jobs"]
        counts[source] = len(original_jobs)
        legacy_keys.update((source, j["id"]) for j in original_jobs if not j.get("first_seen_at"))
    if args.refresh_baidu:
        previous = load_cache(cache, "baidu")
        fresh = crawl(source="baidu", max_pages=1)
        stamp_snapshot(fresh, previous, ledger=cache.parent / "observations.sqlite3")
        save_cache(fresh, cache)
        counts["baidu"] = len(fresh["jobs"])
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
        with urlopen(req, timeout=30) as response:
            data = response.read()
            return data if raw else json.loads(data)

    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "sources": counts,
              "acquisition": {s: "live_one_page" if s == "baidu" and args.refresh_baidu else "existing_real_snapshot" for s in SOURCES}}
    try:
        base = {"sources": ",".join(SOURCES), "include_uncertain": "1"}
        all_jobs = request("/api/jobs", base)
        assert all_jobs["total"] == sum(counts.values())
        assert len({(j["source"], j["id"]) for j in all_jobs["jobs"]}) == all_jobs["total"]
        report["total"] = all_jobs["total"]
        query = {**base, "selector": json.dumps({"technologies": ["Python"], "years": 3, "locations": ["北京", "上海"]})}
        report["rankers"] = {}
        for ranker in RANKERS:
            q = {**query, "ranker": ranker}
            found = request("/api/jobs", q)
            values = [publication_time(j.get("published_at")) if ranker.startswith("published") else j["experience"]["min"] for j in found["jobs"]]
            known = [v for v in values if v is not None]
            assert values == known + [None] * (len(values) - len(known))
            assert known == sorted(known, reverse=ranker.endswith("desc"))
            a = request("/api/jobs", {**q, "limit": 30})
            b = request("/api/jobs", {**q, "offset": 30, "limit": 30})
            assert a["jobs"] + b["jobs"] == found["jobs"][:60]
            exported = request("/api/export", {**q, "format": "json", "limit": 1})
            assert exported["jobs"] == found["jobs"]
            rows = list(csv.DictReader(io.StringIO(request("/api/export", {**q, "format": "csv"}, raw=True).decode("utf-8-sig"))))
            assert [(r["source"], r["id"]) for r in rows] == [(j["source"], j["id"]) for j in found["jobs"]]
            assert [r["match_state"] for r in rows] == [j["match_state"] for j in found["jobs"]]
            report["rankers"][ranker] = {"matched": found["matched_count"], "uncertain": found["uncertain_count"], "pagination_and_exports": "passed"}
        intents = []
        for name, selector in (("真实验收·上海 Python", {"locations": ["上海"], "technologies": ["Python"]}),
                               ("真实验收·北京研发", {"locations": ["北京"], "keywords": ["研发"]})):
            intents.append(request("/api/intents", body={"name": name, "sources": SOURCES, "selector": selector, "ranker": "experience_asc"}))
        selected = [next(j for j in all_jobs["jobs"] if j["source"] == source) for source in ("baidu", "tencent")]
        for job in selected:
            request("/api/favorites", body={"source": job["source"], "id": job["id"], "saved": True})
        stop()
        server = make_server(port=0, cache=cache)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        assert len(request("/api/intents")["intents"]) == 2
        for intent in request("/api/intents")["intents"]:
            result = request("/api/jobs", {"sources": ",".join(intent["sources"]), "selector": json.dumps(intent["selector"]),
                                          "ranker": intent["ranker"], "include_uncertain": "1"})
            assert result["total"] > 0
        saved = request("/api/favorites")
        assert saved["total"] == 2
        before = load_cache(cache, "baidu")
        save_cache({**before, "jobs": [j for j in before["jobs"] if j["id"] != selected[0]["id"]]}, cache)
        archived = request("/api/favorites")
        assert archived["total"] == 2 and any(j["archived"] for j in archived["jobs"])
        save_cache(before, cache)
        report.update(intents_after_restart=2, favorites_after_restart=2, archived_favorite_retained=True,
                      first_seen_unknown_for_legacy=True, fresh_baidu_sample=selected[0] if args.refresh_baidu else None)
        assert all(j["first_seen_at"] is None for j in all_jobs["jobs"] if (j["source"], j["id"]) in legacy_keys)
        assert all(hashlib.sha256(cache_path(s).read_bytes()).hexdigest() == originals[s] for s in SOURCES)
        report["production_caches_unchanged"] = True
        report["status"] = "passed"
        save_cache(report, args.output / "validation.json")
        print(json.dumps({k: report[k] for k in ("status", "total", "rankers", "production_caches_unchanged")}, ensure_ascii=False, indent=2))
        print("报告:", (args.output / "validation.json").resolve())
    finally:
        stop()


if __name__ == "__main__":
    main()
