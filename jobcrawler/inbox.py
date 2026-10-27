"""Per-intention change/read state and quiet, idempotent local digests."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .crawler import DEFAULT_CACHE, save_cache
from .discovery import search_jobs, enrich_job
from .incremental import content_hash
from .personal import PersonalStore, validate_digest, INTENT_SCOPE
from .rankers import rank_jobs, publication_time

CONTENT_FIELDS = ("id", "source", "title", "company", "url", "locations", "requirements", "description",
                  "published_at", "updated_at", "category", "department", "recruitment_type", "experience", "technologies", "details_pending")


def refresh_intent(store, intent, cache):
    result = search_jobs(intent["sources"], intent["selector"], intent["ranker"], cache,
                         include_uncertain=True)
    jobs = result["jobs"] if intent["include_uncertain"] else [j for j in result["jobs"] if j["match_state"] == "matched"]
    now = datetime.now(timezone.utc).isoformat()
    changes = {"new": 0, "updated": 0, "baseline": 0, "source_errors": result["source_errors"]}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM intents WHERE id=?", (intent["id"],)).fetchone()
        current = json.loads(row[0]) if row else {}
        if not row or any(current.get(k) != intent.get(k) for k in (*INTENT_SCOPE, "revision")):
            return {**changes, "superseded": True}
        pending = set(current.get("baseline_sources", []))
        for snapshot in result["snapshots"]:
            source = snapshot["source_id"]
            observed = publication_time(snapshot.get("fetched_at"))
            if observed is None:
                continue
            prior = db.execute("SELECT observed FROM inbox_sources WHERE intent=? AND source=?", (intent["id"], source)).fetchone()
            if prior and observed < prior[0]:
                continue
            baseline = prior is None or source in pending
            db.execute("UPDATE inbox SET current=0 WHERE intent=? AND source=?", (intent["id"], source))
            for job in (j for j in jobs if j["source"] == source):
                key = (intent["id"], source, job["id"])
                fingerprint = content_hash({**{k: job.get(k) for k in CONTENT_FIELDS}, "details_pending": bool(job.get("details_pending"))})
                old = db.execute("SELECT hash, version FROM inbox WHERE intent=? AND source=? AND id=?", key).fetchone()
                payload = json.dumps(job, ensure_ascii=False)
                if not old:
                    kind = "baseline" if baseline else "new"
                    db.execute("INSERT INTO inbox VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (*key, payload, fingerprint, 1, int(baseline), int(baseline), kind, now, 1))
                    changes[kind] += 1
                elif old[0] != fingerprint:
                    db.execute("UPDATE inbox SET payload=?, hash=?, version=version+1, kind='updated', changed_at=?, current=1 WHERE intent=? AND source=? AND id=?",
                               (payload, fingerprint, now, *key))
                    changes["updated"] += 1
                else:
                    db.execute("UPDATE inbox SET payload=?, current=1 WHERE intent=? AND source=? AND id=?", (payload, *key))
            db.execute("INSERT OR REPLACE INTO inbox_sources VALUES (?,?,?)", (intent["id"], source, observed))
            pending.discard(source)
        if pending != set(current.get("baseline_sources", [])):
            current["baseline_sources"] = sorted(pending)
            db.execute("UPDATE intents SET payload=? WHERE id=?", (json.dumps(current, ensure_ascii=False), intent["id"]))
    return changes


def preview_intent(store, body, cache):
    proposed = store.prepare_intent(body)
    old = store.intent(proposed["id"]) if body.get("id") else None
    result = search_jobs(proposed["sources"], proposed["selector"], proposed["ranker"], cache, include_uncertain=True)
    matches = result["jobs"] if proposed["include_uncertain"] else [j for j in result["jobs"] if j["match_state"] == "matched"]
    keys = {(j["source"], j["id"]) for j in matches}
    with store.connect() as db:
        rows = db.execute("SELECT source,id,version,read_version,current FROM inbox WHERE intent=?", (proposed["id"],)).fetchall()
    return {"scope_changed": bool(old) and any(old[k] != proposed[k] for k in INTENT_SCOPE),
            "expected_revision": old.get("revision", 0) if old else 0,
            "matching": len(keys), "uncertain": sum(j["match_state"] == "uncertain" for j in matches),
            "newly_matching": len(keys - {(r[0], r[1]) for r in rows if r[4]}),
            "leaving": sum(bool(r[4]) and (r[0], r[1]) not in keys for r in rows),
            "unread_preserved": sum(r[2] > r[3] for r in rows), "source_errors": result["source_errors"]}


def application_fields(store, jobs):
    records = {(a["source"], a["id"]): a for a in store.applications()}
    return [{**job, "application_status": records.get((job["source"], job["id"]), {}).get("status", "none"),
             "application_notes": records.get((job["source"], job["id"]), {}).get("notes", "")} for job in jobs]


def read_inbox(store, ident, state="unread", current_only=False):
    intent = store.intent(ident)
    if state not in ("all", "unread", "read", "updated"):
        raise ValueError("收件箱状态必须为 all/unread/read/updated")
    with store.connect() as db:
        rows = db.execute("SELECT payload,version,read_version,kind,changed_at,current FROM inbox WHERE intent=?", (ident,)).fetchall()
    favorites = {(r["source"], r["id"]): r for r in store.favorites()}
    jobs = []
    for payload, version, read_version, kind, changed_at, current in rows:
        if current_only and not current:
            continue
        job = json.loads(payload)
        job = enrich_job(job, {}, favorites.get((job["source"], job["id"])), archived=not current)
        job.update(inbox_version=version, read_state="read" if read_version >= version else "unread",
                   change_kind=kind, changed_at=changed_at, currently_matching=bool(current), intent_name=intent["name"])
        jobs.append(job)
    counts = {"unread": sum(j["read_state"] == "unread" for j in jobs),
              "read": sum(j["read_state"] == "read" for j in jobs),
              "updated": sum(j["change_kind"] == "updated" for j in jobs)}
    jobs = [j for j in jobs if state == "all" or j["read_state"] == state or state == "updated" and j["change_kind"] == "updated"]
    jobs = application_fields(store, rank_jobs(jobs, intent["ranker"]))
    return {"intent": intent, "counts": counts, "total": len(jobs), "jobs": jobs, "ranker": intent["ranker"]}


def generate_digest(store, intent_id, directory, now=None, excluded_sources=()):
    now = now or datetime.now(timezone.utc)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM intents WHERE id=?", (intent_id,)).fetchone()
        if not row:
            return None
        intent = json.loads(row[0])
        config = validate_digest(intent.get("digest", {}))
        if not config["enabled"]:
            return None
        hour = now.astimezone(ZoneInfo("Asia/Shanghai")).hour
        start, end = config["quiet_start"], config["quiet_end"]
        if start != end and (start <= hour < end if start < end else hour >= start or hour < end):
            return None
        last = db.execute("SELECT created_at FROM notifications WHERE intent=? ORDER BY created_at DESC LIMIT 1", (intent_id,)).fetchone()
        interval = {"hourly": 3600, "daily": 86400, "every_3_days": 259200}[config["frequency"]]
        if last and now.timestamp() - publication_time(last[0]) < interval:
            return None
        rows = db.execute("SELECT source,id,payload,version,kind FROM inbox WHERE intent=? AND current=1 AND version>read_version AND version>notified_version AND kind!='baseline'", (intent_id,)).fetchall()
        entries = []
        for source, ident, payload, version, kind in rows:
            if source in excluded_sources:
                continue
            job = json.loads(payload)
            if kind == "updated" and not config["include_updates"] or job["match_state"] == "uncertain" and not config["include_uncertain"]:
                continue
            entries.append({"source": source, "id": ident, "version": version, "kind": kind,
                            "title": job["title"], "company": job["company"], "url": job["url"],
                            "match_state": job["match_state"], "last_seen_at": job.get("last_seen_at")})
        if not entries:
            return None
        digest_id = uuid.uuid4().hex
        result = {"id": digest_id, "intent_id": intent_id, "intent_name": intent["name"], "created_at": now.isoformat(),
                  "new_count": sum(e["kind"] == "new" for e in entries),
                  "updated_count": sum(e["kind"] == "updated" for e in entries), "entries": entries,
                  "file": str((Path(directory) / (digest_id + ".json")).resolve())}
        # File creation must succeed before advancing the notification watermark.
        save_cache(result, result["file"])
        db.execute("INSERT INTO notifications (id,intent,created_at,payload) VALUES (?,?,?,?)",
                   (digest_id, intent_id, result["created_at"], json.dumps(result, ensure_ascii=False)))
        for entry in entries:
            db.execute("UPDATE inbox SET notified_version=? WHERE intent=? AND source=? AND id=?",
                       (entry["version"], intent_id, entry["source"], entry["id"]))
    return result


def process_inbox(cache=DEFAULT_CACHE, store=None, intent_id=None, now=None, excluded_sources=()):
    store = store or PersonalStore(Path(cache).parent / "personal.sqlite3")
    intents = [store.intent(intent_id)] if intent_id else store.intents()
    result = {"intents": {}, "digest_ids": [], "errors": []}
    for intent in intents:
        try:
            changes = refresh_intent(store, intent, cache)
            result["intents"][intent["id"]] = changes
            failed = set(excluded_sources) | {error["source"] for error in changes["source_errors"]}
            # Failed sources cannot turn stale cached unread items into new alerts.
            digest = generate_digest(store, intent["id"], Path(cache).parent / "digests", now=now, excluded_sources=failed)
            if digest:
                result["digest_ids"].append(digest["id"])
        except Exception as exc:
            result["errors"].append({"intent_id": intent["id"], "error": str(exc)})
    return result
