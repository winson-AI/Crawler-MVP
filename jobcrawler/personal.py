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


class PersonalStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS favorites (source TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(source,id))")

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

    def save_intent(self, body):
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
        ident = body.get("id") or uuid.uuid4().hex
        if not isinstance(ident, str) or len(ident) > 80:
            raise ValueError("意向 ID 无效")
        payload = {"id": ident, "name": name.strip(), "sources": list(dict.fromkeys(sources)),
                   "selector": selector, "ranker": ranker, "include_uncertain": uncertain,
                   "updated_at": datetime.now(timezone.utc).isoformat()}
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM intents WHERE id=?", (ident,)).fetchone()
            if body.get("id") and not exists:
                raise ValueError("意向已不存在，请另存为新意向")
            if not exists and db.execute("SELECT count(*) FROM intents").fetchone()[0] >= 50:
                raise ValueError("最多保存 50 个求职意向")
            db.execute("INSERT OR REPLACE INTO intents VALUES (?,?)", (ident, json.dumps(payload, ensure_ascii=False)))
        return payload

    def delete_intent(self, ident):
        if not isinstance(ident, str):
            raise ValueError("意向 ID 无效")
        with self.connect() as db:
            db.execute("DELETE FROM intents WHERE id=?", (ident,))

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
