"""Local, transactional storage for named intentions and saved job snapshots."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .selectors import validate_selector
from .rankers import rank_jobs
from .sources import get_source

APPLICATION_STATES = ("none", "planned", "applied", "interview", "offer", "rejected", "withdrawn")
DIGEST_DEFAULTS = {"enabled": False, "frequency": "daily", "include_updates": True,
                   "include_uncertain": False, "quiet_start": 22, "quiet_end": 8}
INTENT_SCOPE = ("sources", "selector", "include_uncertain")


def validate_digest(value):
    if not isinstance(value, dict) or set(value) - set(DIGEST_DEFAULTS):
        raise ValueError("摘要配置字段无效")
    result = {**DIGEST_DEFAULTS, **value}
    for key in ("enabled", "include_updates", "include_uncertain"):
        if not isinstance(result[key], bool):
            raise ValueError(key + " 必须为布尔值")
    if result["frequency"] not in ("hourly", "daily", "every_3_days"):
        raise ValueError("摘要频率必须为 hourly/daily/every_3_days")
    for key in ("quiet_start", "quiet_end"):
        if isinstance(result[key], bool) or not isinstance(result[key], int) or not 0 <= result[key] <= 23:
            raise ValueError("免打扰小时应为 0–23")
    return result


def matching_scope(intent):
    selector = validate_selector(intent.get("selector", {}))
    selector.pop("preferences", None)
    return {"sources": sorted(intent.get("sources", [])), "selector": selector,
            "include_uncertain": intent.get("include_uncertain", True)}


class PersonalStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS favorites (source TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(source,id))")
            db.execute("CREATE TABLE IF NOT EXISTS inbox (intent TEXT, source TEXT, id TEXT, payload TEXT NOT NULL, hash TEXT NOT NULL, version INTEGER NOT NULL, read_version INTEGER NOT NULL, notified_version INTEGER NOT NULL, kind TEXT NOT NULL, changed_at TEXT NOT NULL, current INTEGER NOT NULL, PRIMARY KEY(intent,source,id))")
            db.execute("CREATE TABLE IF NOT EXISTS inbox_changes (intent TEXT, source TEXT, id TEXT, version INTEGER, payload TEXT NOT NULL, PRIMARY KEY(intent,source,id,version))")
            db.execute("CREATE TABLE IF NOT EXISTS inbox_sources (intent TEXT, source TEXT, observed REAL NOT NULL, PRIMARY KEY(intent,source))")
            db.execute("CREATE TABLE IF NOT EXISTS applications (source TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(source,id))")
            db.execute("CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, intent TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS source_health (source TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def intents(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM intents ORDER BY rowid")]

    def prepare_intent(self, body):
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("意向名称应为 1–80 字符")
        sources = body.get("sources")
        if not isinstance(sources, list) or not sources or len(sources) > 60 or any(not isinstance(s, str) for s in sources):
            raise ValueError("至少选择一个招聘来源")
        for source in sources:
            get_source(source)
        selector = validate_selector(body.get("selector", {}))
        ranker = body.get("ranker", "published_desc")
        rank_jobs([], ranker)
        uncertain = body.get("include_uncertain", True)
        if not isinstance(uncertain, bool):
            raise ValueError("include_uncertain 必须为布尔值")
        collapse = body.get("collapse_duplicates", False)
        if not isinstance(collapse, bool):
            raise ValueError("collapse_duplicates 必须为布尔值")
        ident = body.get("id") or uuid.uuid4().hex
        if not isinstance(ident, str) or len(ident) > 80:
            raise ValueError("意向 ID 无效")
        payload = {"id": ident, "name": name.strip(), "sources": list(dict.fromkeys(sources)),
                   "selector": selector, "ranker": ranker, "collapse_duplicates": collapse, "include_uncertain": uncertain,
                   "updated_at": datetime.now(timezone.utc).isoformat()}
        return payload

    def save_intent(self, body):
        payload = self.prepare_intent(body)
        ident = payload["id"]
        mode = body.get("edit_mode", "preserve")
        if mode not in ("preserve", "restart"):
            raise ValueError("edit_mode 必须为 preserve 或 restart")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM intents WHERE id=?", (ident,)).fetchone()
            exists = row is not None
            old = json.loads(row[0]) if row else {}
            revision = old.get("revision", 0)
            expected = body.get("expected_revision")
            if expected is not None and (type(expected) is not int or expected != revision):
                raise ValueError("意向已被修改，请重新载入并预览")
            if mode == "restart" and (not exists or expected is None or body.get("restart_confirmed") is not True):
                raise ValueError("重新建立基线需要当前版本和明确确认")
            payload["digest"] = validate_digest(body.get("digest", old.get("digest", {})))
            if body.get("id") and not exists:
                raise ValueError("意向已不存在，请另存为新意向")
            if not exists and db.execute("SELECT count(*) FROM intents").fetchone()[0] >= 50:
                raise ValueError("最多保存 50 个求职意向")
            payload["revision"] = revision + 1
            changed = bool(old) and matching_scope(old) != matching_scope(payload)
            payload["baseline_sources"] = old.get("baseline_sources", [])
            if old.get("previous_settings"):
                payload["previous_settings"] = old["previous_settings"]
            if changed:
                payload["previous_settings"] = {k: old[k] for k in INTENT_SCOPE}
                payload["baseline_sources"] = payload["sources"][:]
                db.execute("UPDATE inbox SET current=0 WHERE intent=?", (ident,))
            if mode == "restart":
                db.execute("DELETE FROM inbox_changes WHERE intent=?", (ident,))
                db.execute("DELETE FROM inbox WHERE intent=?", (ident,))
                db.execute("DELETE FROM inbox_sources WHERE intent=?", (ident,))
                payload.pop("previous_settings", None)
                payload["baseline_sources"] = payload["sources"][:]
            db.execute("INSERT OR REPLACE INTO intents VALUES (?,?)", (ident, json.dumps(payload, ensure_ascii=False)))
        return payload

    def undo_intent(self, ident, expected_revision):
        if not isinstance(ident, str):
            raise ValueError("意向 ID 无效")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM intents WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ValueError("求职意向不存在")
            intent = json.loads(row[0])
            if type(expected_revision) is not int or expected_revision != intent.get("revision", 0):
                raise ValueError("意向已被修改，请重新载入")
            previous = intent.pop("previous_settings", None)
            if not previous:
                raise ValueError("没有可撤销的条件修改")
            intent.update(previous, revision=expected_revision + 1,
                          updated_at=datetime.now(timezone.utc).isoformat())
            intent["baseline_sources"] = intent["sources"][:]
            db.execute("UPDATE inbox SET current=0 WHERE intent=?", (ident,))
            db.execute("UPDATE intents SET payload=? WHERE id=?", (json.dumps(intent, ensure_ascii=False), ident))
        return intent

    def delete_intent(self, ident):
        if not isinstance(ident, str):
            raise ValueError("意向 ID 无效")
        with self.connect() as db:
            db.execute("DELETE FROM inbox_changes WHERE intent=?", (ident,))
            db.execute("DELETE FROM intents WHERE id=?", (ident,))
            db.execute("DELETE FROM inbox WHERE intent=?", (ident,))
            db.execute("DELETE FROM inbox_sources WHERE intent=?", (ident,))

    def intent(self, ident):
        if not isinstance(ident, str):
            raise ValueError("请选择已保存的求职意向")
        with self.connect() as db:
            row = db.execute("SELECT payload FROM intents WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("求职意向不存在")
        return json.loads(row[0])

    def digest_settings(self, ident, settings):
        settings = validate_digest(settings)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM intents WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ValueError("求职意向不存在")
            intent = json.loads(row[0])
            intent["digest"] = settings
            db.execute("UPDATE intents SET payload=? WHERE id=?", (json.dumps(intent, ensure_ascii=False), ident))
        return settings

    def mark_read(self, intent, source, ident, version, read=True):
        self.intent(intent)
        if not isinstance(read, bool) or isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("阅读状态和版本无效")
        with self.connect() as db:
            row = db.execute("SELECT version FROM inbox WHERE intent=? AND source=? AND id=?", (intent, source, ident)).fetchone()
            if not row or version > row[0]:
                raise ValueError("职位或版本不存在，请刷新收件箱")
            if read:
                db.execute("UPDATE inbox SET read_version=max(read_version,?) WHERE intent=? AND source=? AND id=?", (version, intent, source, ident))
            else:
                db.execute("UPDATE inbox SET read_version=0 WHERE intent=? AND source=? AND id=?", (intent, source, ident))

    def applications(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM applications ORDER BY rowid DESC")]

    def application(self, job, status, notes):
        if status not in APPLICATION_STATES or not isinstance(notes, str) or len(notes) > 2000:
            raise ValueError("投递状态无效或备注超过 2000 字符")
        record = {"source": job["source"], "id": job["id"], "status": status, "notes": notes,
                  "job": job, "saved_at": datetime.now(timezone.utc).isoformat()}
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO applications VALUES (?,?,?)", (job["source"], job["id"], json.dumps(record, ensure_ascii=False)))
        return record

    def saved_job(self, source, ident):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM inbox WHERE source=? AND id=? ORDER BY changed_at DESC LIMIT 1", (source, ident)).fetchone()
        if row:
            return json.loads(row[0])
        return next((r["job"] for r in self.favorites() + self.applications() if r["source"] == source and r["id"] == ident), None)

    def notifications(self):
        with self.connect() as db:
            return [{**json.loads(row[0]), "read": bool(row[1])} for row in db.execute("SELECT payload, read FROM notifications ORDER BY created_at DESC LIMIT 100")]

    def read_notification(self, ident):
        if not isinstance(ident, str):
            raise ValueError("通知 ID 无效")
        with self.connect() as db:
            db.execute("UPDATE notifications SET read=1 WHERE id=?", (ident,))

    def favorites(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM favorites ORDER BY rowid DESC")]

    def favorite(self, source, ident, job=None):
        with self.connect() as db:
            if job is None:
                db.execute("DELETE FROM favorites WHERE source=? AND id=?", (source, ident))
            else:
                # Repeated saves preserve the original saved time and snapshot.
                payload = {"source": source, "id": ident, "job": job,
                           "saved_at": datetime.now(timezone.utc).isoformat()}
                db.execute("INSERT OR IGNORE INTO favorites VALUES (?,?,?)",
                           (source, ident, json.dumps(payload, ensure_ascii=False)))
