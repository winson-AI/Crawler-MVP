"""Real ATS HTTP -> template recovery -> mount -> serve filters/exports acceptance.

Runs against a local service; deliberately breaks the array path, never imports a
document or supplies a cached page. Existing target sources are refreshed in place.
"""
import argparse
import csv
import io
import ipaddress
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from verify_sources import verify_pipeline
from jobcrawler.crawler import save_cache
from jobcrawler.rankers import RANKERS, rank_jobs
from jobcrawler.selectors import select_jobs

TARGETS = [
    ("greenhouse", "Greenhouse", "https://job-boards.greenhouse.io/greenhouse"),
    ("lever", "Zoox", "https://jobs.lever.co/zoox?department=Software&location=Boston%2C+MA"),
    ("ashby", "Ashby", "https://jobs.ashbyhq.com/Ashby"),
]


def require_network(events, started):
    if not events:
        raise RuntimeError("缺少本轮真实 HTTP 证据")
    for event in events:
        if (event.get("http_status") != 200 or not event.get("tls_verified")
                or not event.get("response_bytes") or not event.get("response_sha256")
                or event.get("dns_mode") not in ("system", "cloudflare_doh")
                or event.get("requested_at", "") < started):
            raise RuntimeError("响应不是本轮成功的 HTTPS 全文请求")
        addresses = event.get("resolved_addresses") or []
        if (not addresses or event.get("connected_ip") not in addresses
                or any(not ipaddress.ip_address(ip).is_global for ip in addresses)):
            raise RuntimeError("连接未固定到已校验的公网地址")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("output") / ("ats-live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    args = parser.parse_args()
    base = args.server.rstrip("/")
    if urlsplit(base).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("验收脚本只连接本机 serve")
    args.output.mkdir(parents=True, exist_ok=True)

    def request(route, body=None, text=False):
        req = Request(base + route, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"} if body is not None else {})
        with urlopen(req, timeout=45) as reply:
            content = reply.read().decode("utf-8")
        return content if text else json.loads(content)

    def summary(source):
        return request("/api/status?" + urlencode({"source": source}))["snapshot"]

    if request("/api/status")["running"]:
        raise RuntimeError("已有抓取任务，未启动并发验收")
    before = {s["id"]: summary(s["id"]) for s in request("/api/sources")["sources"]}
    save_cache({"sources": before, "templates": request("/api/templates")}, args.output / "before.json")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "server": base,
              "uses_document_import": False, "uses_listing_cache_for_crawl": False, "sources": []}
    targets = set()
    for provider, company, url in TARGETS:
        result = {"provider": provider, "url": url, "company": company}
        report["sources"].append(result)
        try:
            matched = request("/api/templates/match", {"url": url})
            config = matched["config"]
            if config.get("template_id") != provider or not config.get("fetch_url"):
                raise RuntimeError("网址未匹配到预期公开接口模板")
            config.update(mode="http", name=company + "·公开接口（实时 HTTP）", company=company,
                          parser="json", auto_adapt=True, items="obsolete_layout.jobs")
            identity = config["id"]
            targets.add(identity)
            result.update(source_id=identity, matched_kind=matched["template"]["kind"],
                          fetch_url=config["fetch_url"], fault_injected_items=config["items"])
            started = datetime.now(timezone.utc).isoformat()
            preview = request("/api/sources/preview", {"config": config})
            require_network(preview.get("network_requests"), started)
            if preview["acquisition"] != "network" or not preview.get("adaptation"):
                raise RuntimeError("未通过真实联网自动适配")
            result["preview"] = {k: preview[k] for k in ("collected", "adaptation", "network_requests", "acquisition")}
            mounted = request("/api/sources/custom", {"config": config})
            require_network(mounted.get("network_requests"), started)
            if not mounted.get("mounted") or not mounted.get("refreshing") or not mounted.get("adaptation"):
                raise RuntimeError("未完成实际联网挂载")
            result["mount"] = {k: mounted[k] for k in ("collected", "adaptation", "network_requests", "acquisition")}
            deadline = time.monotonic() + 90
            while True:
                state = request("/api/status?" + urlencode({"source": identity}))
                if not state["running"]:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("后台抓取超时")
                time.sleep(0.25)
            snapshot = state["snapshot"]
            if state["error"] or state["configuration_changed"]:
                raise RuntimeError(state["error"] or "已保存配置与快照版本不一致")
            require_network(snapshot.get("network_requests"), started)
            if snapshot.get("acquisition") != "network" or snapshot.get("scope") == "document" or not snapshot.get("complete"):
                raise RuntimeError("快照不是本轮完整的 HTTP 列表范围")
            exported = request("/api/export?" + urlencode({"source": identity}))
            checks = verify_pipeline({**snapshot, "jobs": exported["jobs"]}, args.output)
            selector = checks["selector"]
            for ranker in RANKERS:
                query = urlencode({"source": identity, "selector": json.dumps(selector), "ranker": ranker})
                actual = request("/api/jobs?" + query)
                expected = rank_jobs(select_jobs(exported["jobs"], selector), ranker)
                if [j["id"] for j in actual["jobs"]] != [j["id"] for j in expected]:
                    raise RuntimeError("serve 组合筛选或排序结果不一致")
                if request("/api/export?" + query)["jobs"] != actual["jobs"]:
                    raise RuntimeError("serve JSON 导出不一致")
                rows = list(csv.DictReader(io.StringIO(request("/api/export?" + query + "&format=csv", text=True).lstrip("\ufeff"))))
                if len(rows) != len(expected):
                    raise RuntimeError("serve CSV 导出数量不一致")
            learned = request("/api/templates/match", {"url": url})
            if learned["template"]["kind"] != "learned" or learned["config"]["mode"] != "http" or learned["config"]["items"] == "obsolete_layout.jobs":
                raise RuntimeError("自动适配规则未持久保存复用")
            result.update(status="passed", snapshot=snapshot, checks=checks,
                          serve_filters_and_exports=True, learned_rules_reused=True)
            print(f"{provider}: PASS · {snapshot['collected']} 条 · HTTP/DNS/自动适配/入库/筛选/导出", flush=True)
        except Exception as exc:
            result.update(status="failed", error=str(exc))
            print(f"{provider}: FAIL · {exc}", flush=True)
        save_cache(report, args.output / "validation.json")
    report["untargeted_sources_unchanged"] = all(summary(source) == old for source, old in before.items() if source not in targets)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = report["untargeted_sources_unchanged"] and all(r.get("status") == "passed" for r in report["sources"])
    save_cache(report, args.output / "validation.json")
    print("报告: " + str((args.output / "validation.json").resolve()), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
