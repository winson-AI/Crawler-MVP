"""Cached source health and a jobseeker summary using existing matching state."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .crawler import cache_path, load_cache
from .personal import PersonalStore
from .rankers import publication_time
from .sources import list_sources


def record_health(cache, source, status, started_at, error=None, new_count=None):
    """Health storage must not undo a successful cache/history commit."""
    try:
        store = PersonalStore(Path(cache).parent / "personal.sqlite3")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM source_health WHERE source=?", (source,)).fetchone()
            old = json.loads(row[0]) if row else {}
            if (publication_time(old.get("started_at")) or 0) > publication_time(started_at):
                return None
            value = {"status": status, "started_at": started_at,
                     "updated_at": datetime.now(timezone.utc).isoformat(),
                     "error": str(error).split("\n", 1)[0][:350] if error else None,
                     "new_count": new_count if status == "ok" else None}
            db.execute("INSERT OR REPLACE INTO source_health VALUES (?,?)", (source, json.dumps(value, ensure_ascii=False)))
    except (OSError, ValueError, sqlite3.Error) as exc:
        return str(exc)
    return None


def run_summary(report):
    rows = list(report.get("sources", {}).values())
    ok = sum(r.get("status") == "ok" for r in rows)
    failed = sum(r.get("status") in ("error", "paused") for r in rows)
    return {"started_at": report.get("started_at"), "finished_at": report.get("finished_at"),
            "successful": ok, "failed": failed, "total": len(rows),
            "outcome": "running" if not report.get("finished_at") else "no_fresh_data" if not ok else
                       "partial_success" if failed else "refreshed",
            "new_count": report.get("new_count") if ok else None}


def source_health(cache, reports=None, store=None):
    cache = Path(cache)
    store = store or PersonalStore(cache.parent / "personal.sqlite3")
    reports = Path(reports) if reports is not None else cache.parent.parent / "output" / "incremental"
    events, latest, warnings = {}, None, []
    paths = sorted(reports.glob("*/report.json"), reverse=True)[:100]
    for path in paths:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            # Isolated acceptance runs must never affect another cache's health.
            if Path(report.get("config", {}).get("cache", "")).resolve() != cache.resolve():
                continue
            when = report.get("finished_at") or report.get("started_at")
            stamp = publication_time(when)
            if stamp is None:
                continue
            if latest is None or stamp > latest[0]:
                latest = (stamp, run_summary(report))
            for source, result in report.get("sources", {}).items():
                started = result.get("attempt_started_at") or report.get("started_at")
                attempt = publication_time(started) or stamp
                if source not in events or attempt > events[source][0]:
                    events[source] = (attempt, {"status": result.get("status"), "started_at": started,
                        "updated_at": when, "new_count": result.get("new_count"), "error": result.get("error")})
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            warnings.append({"report": str(path), "error": str(exc)})
    with store.connect() as db:
        for source, payload in db.execute("SELECT source,payload FROM source_health"):
            event = json.loads(payload)
            stamp = publication_time(event.get("started_at")) or 0
            if source not in events or stamp >= events[source][0]:
                events[source] = (stamp, event)
    rows = []
    for source in list_sources():
        ident = source["id"]
        event = events.get(ident, (0, {}))[1]
        mode = source.get("config", {}).get("mode", "http")
        try:
            snapshot = load_cache(cache_path(ident, cache), ident)
            cache_error = None
        except (OSError, ValueError, TypeError) as exc:
            snapshot, cache_error = {}, str(exc)
        status = event.get("status") or ("cached" if snapshot.get("fetched_at") else "never")
        if status in ("waiting_for_human", "paused"):
            status = "needs_human"
        if status == "running" and datetime.now(timezone.utc).timestamp() - (publication_time(event.get("updated_at")) or 0) > 3600:
            status = "unconfirmed"
        if cache_error:
            status = "cache_error"
        action = "查看刷新入口" if mode not in ("document", "browser") else "查看文档导入入口" if mode == "document" else "查看浏览器连接说明"
        if status == "needs_human":
            action = "查看人工验证说明"
        guidance = "可在下方选择来源并刷新；失败时仍保留旧记录。"
        if mode == "document":
            guidance = "请在高级配置中导入新的职位文档；旧文档不能作为本轮联网成功。"
        elif mode == "browser":
            guidance = "请查看高级配置中的原 Chrome 连接说明，在原窗口完成授权或登录。"
        if status == "needs_human":
            guidance = "请先在原抓取窗口手动完成验证；已暂停的增量任务须恢复原运行，不能用重新刷新替代断点恢复。"
        rows.append({"source": ident, "name": source["name"], "mode": mode,
            "schedulable": mode not in ("document", "browser"), "status": status,
            "last_attempt_at": event.get("started_at"), "last_success_at": snapshot.get("fetched_at"),
            "collected": len(snapshot.get("jobs", [])), "complete": snapshot.get("complete", False),
            "scope": snapshot.get("scope", "listing"), "new_count": event.get("new_count") if status == "ok" else None,
            "error": (str(cache_error or event.get("error") or "").split("\n", 1)[0])[:350] or None,
            "action": action, "guidance": guidance})
    return {"sources": rows, "latest_run": latest[1] if latest else None, "warnings": warnings}


def home_summary(store, cache):
    from .inbox import refresh_intent, read_inbox
    intents = []
    for intent in store.intents():
        changes = refresh_intent(store, intent, cache)
        inbox = read_inbox(store, intent["id"], "all")
        matching = [j for j in inbox["jobs"] if j["currently_matching"]]
        intents.append({"id": intent["id"], "name": intent["name"], "revision": intent.get("revision", 0),
            "matching": len(matching), "unread": sum(j["read_state"] == "unread" for j in matching),
            "updated": sum(j["change_kind"] == "updated" and j["read_state"] == "unread" for j in matching),
            "uncertain": sum(j["match_state"] == "uncertain" for j in matching), "source_errors": changes["source_errors"]})
    applications = store.applications()
    return {"intents": intents, "favorites": len(store.favorites()),
            "followup": {s: sum(a["status"] == s for a in applications) for s in ("planned", "applied", "interview")},
            "unread_notifications": sum(not n["read"] for n in store.notifications())}
