import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jobcrawler.crawler import cache_path, save_cache
from jobcrawler.inbox import refresh_intent, read_inbox, generate_digest, process_inbox
from jobcrawler.models import build_job
from jobcrawler.personal import PersonalStore


def job(ident="one", source="baidu", **changes):
    return {**build_job(id=ident, title="Python研发", source=source, company=source, url="https://example.com/"+ident,
                       locations=["北京"], requirements="3年以上 Python", description="后端研发"), **changes}


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "jobs.json"
        self.store = PersonalStore(self.root / "personal.sqlite3")
        self.intent = self.store.save_intent({"name": "北京Python", "sources": ["baidu"], "selector": {"technologies": ["Python"]}})
        self.now = datetime(2026, 10, 23, 9, tzinfo=timezone.utc)
        self.write([job()])
        refresh_intent(self.store, self.intent, self.cache)

    def write(self, jobs, source="baidu", fetched="2026-10-23T08:00:00+00:00"):
        save_cache({"source_id": source, "jobs": jobs, "fetched_at": fetched, "complete": True, "collected": len(jobs)}, cache_path(source, self.cache))

    def enable(self, **changes):
        self.store.digest_settings(self.intent["id"], {"enabled": True, "frequency": "hourly", "quiet_start": 0, "quiet_end": 0, **changes})

    def refresh(self):
        return refresh_intent(self.store, self.store.intent(self.intent["id"]), self.cache)

    def new(self):
        self.write([job(), job("two")])
        self.refresh()

    def test_baseline_is_read_and_new_is_per_intent_not_global_seen_ids(self):
        self.assertEqual(read_inbox(self.store, self.intent["id"])["total"], 0)
        self.new()
        self.assertEqual(read_inbox(self.store, self.intent["id"])["total"], 1)
        other = self.store.save_intent({"name": "另一个意向", "sources": ["baidu"], "selector": {}})
        refresh_intent(self.store, other, self.cache)
        self.assertEqual(read_inbox(self.store, other["id"])["total"], 0)
        self.assertEqual(read_inbox(self.store, self.intent["id"])["jobs"][0]["change_kind"], "new")

    def test_update_reopens_read_item_and_stale_ack_does_not_swallow_new_version(self):
        self.write([job(requirements="5年以上 Python")])
        self.refresh()
        self.store.mark_read(self.intent["id"], "baidu", "one", 1)
        item = read_inbox(self.store, self.intent["id"])["jobs"][0]
        self.assertEqual((item["inbox_version"], item["change_kind"], item["read_state"]), (2, "updated", "unread"))
        self.refresh()
        self.assertEqual(read_inbox(self.store, self.intent["id"])["jobs"][0]["inbox_version"], 2)
        self.store.mark_read(self.intent["id"], "baidu", "one", 2)
        self.assertEqual(read_inbox(self.store, self.intent["id"])["total"], 0)

    def test_observation_and_personal_changes_are_not_job_updates(self):
        self.write([job(last_seen_at="2026-10-24", details_fetched_at="2026-10-24", first_seen_at="2026-10-20")])
        self.refresh()
        self.store.application(job(), "applied", "已在官网提交")
        self.assertEqual(read_inbox(self.store, self.intent["id"])["total"], 0)
        all_items = read_inbox(self.store, self.intent["id"], "all")["jobs"]
        self.assertEqual(all_items[0]["application_status"], "applied")
        self.write([])
        self.refresh()
        archived = read_inbox(self.store, self.intent["id"], "all")["jobs"][0]
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["application_notes"], "已在官网提交")

    def test_older_snapshot_cannot_roll_back_current_inbox(self):
        self.write([job(), job("two")], fetched="2026-10-24T08:00:00+00:00")
        self.refresh()
        self.write([job(requirements="old Python")], fetched="2026-10-23T08:00:00+00:00")
        result = self.refresh()
        self.assertEqual(result["updated"], 0)
        item = read_inbox(self.store, self.intent["id"])["jobs"][0]
        self.assertEqual(item["id"], "two")
        self.assertTrue(item["currently_matching"])

    def test_criteria_edit_rebaselines_but_rename_and_rank_change_preserve_unread(self):
        self.new()
        changed = self.store.save_intent({**self.store.intent(self.intent["id"]), "name": "新名称", "ranker": "experience_desc"})
        self.assertEqual(read_inbox(self.store, changed["id"])["total"], 1)
        changed = self.store.save_intent({**changed, "selector": {"keywords": ["研发"]}})
        refresh_intent(self.store, changed, self.cache)
        self.assertEqual(read_inbox(self.store, changed["id"])["total"], 0)

    def test_digest_frequency_quiet_time_and_repeat_are_idempotent(self):
        self.enable()
        self.assertIsNone(generate_digest(self.store, self.intent["id"], self.root / "digests", self.now))
        self.new()
        first = generate_digest(self.store, self.intent["id"], self.root / "digests", self.now)
        self.assertEqual(first["new_count"], 1)
        self.assertTrue(Path(first["file"]).exists())
        self.assertIsNone(generate_digest(self.store, self.intent["id"], self.root / "digests", self.now+timedelta(hours=2)))
        self.store.mark_read(self.intent["id"], "baidu", "two", 1, False)
        self.assertIsNone(generate_digest(self.store, self.intent["id"], self.root / "digests", self.now+timedelta(hours=3)))
        self.write([job(), job("two", requirements="Python5年以上")])
        self.refresh()
        self.assertIsNone(generate_digest(self.store, self.intent["id"], self.root / "digests", self.now+timedelta(minutes=30)))
        self.enable(quiet_start=22, quiet_end=8)
        self.assertIsNone(generate_digest(self.store, self.intent["id"], self.root / "digests", self.now.replace(hour=15)))
        second = generate_digest(self.store, self.intent["id"], self.root / "digests", self.now+timedelta(hours=2))
        self.assertEqual(second["updated_count"], 1)

    def test_digest_file_failure_can_retry_and_concurrent_checks_create_one_notification(self):
        self.enable()
        self.new()
        with patch("jobcrawler.inbox.save_cache", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                generate_digest(self.store, self.intent["id"], self.root / "digests", self.now)
        self.assertEqual(self.store.notifications(), [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: generate_digest(self.store, self.intent["id"], self.root / "digests", self.now), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.store.notifications()), 1)

    def test_update_and_uncertainty_preferences_and_unsubscribe(self):
        self.enable(include_updates=False, include_uncertain=False)
        self.write([job(requirements="Python5年以上"), job("pending", title="研发", requirements="", description="", details_pending=True)])
        self.refresh()
        self.assertIsNone(generate_digest(self.store, self.intent["id"], self.root / "digests", self.now))
        self.enable(include_updates=True, include_uncertain=True)
        digest = generate_digest(self.store, self.intent["id"], self.root / "digests", self.now)
        self.assertEqual((digest["new_count"], digest["updated_count"]), (1, 1))
        self.enable(enabled=False)
        self.write([job(), job("three")])
        self.refresh()
        self.assertIsNone(generate_digest(self.store, self.intent["id"], self.root / "digests", self.now+timedelta(days=2)))

    def test_failed_live_sync_cannot_notify_from_its_preserved_cache(self):
        self.enable()
        self.new()
        result = process_inbox(self.cache, self.store, now=self.now, excluded_sources=["baidu"])
        self.assertEqual(result["digest_ids"], [])
        self.assertEqual(read_inbox(self.store, self.intent["id"])["total"], 1)
        result = process_inbox(self.cache, self.store, now=self.now)
        self.assertEqual(len(result["digest_ids"]), 1)

    def test_failed_source_baselines_after_recovery_and_does_not_block_healthy_digest(self):
        self.intent = self.store.save_intent({**self.intent, "sources": ["baidu", "tencent"]})
        cache_path("tencent", self.cache).write_text("invalid")
        self.refresh()
        self.enable()
        self.new()
        result = process_inbox(self.cache, self.store, now=self.now)
        self.assertEqual(len(result["digest_ids"]), 1)
        self.write([job("legacy", "tencent")], source="tencent")
        self.refresh()
        self.assertEqual(read_inbox(self.store, self.intent["id"])["total"], 1)
