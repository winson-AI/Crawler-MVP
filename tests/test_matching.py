"""Behavior checks for hard/soft matching, canonical facts, folding and changes."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from jobcrawler.models import build_job
from jobcrawler.fields import normalize_fields, parse_salary, field_changes
from jobcrawler.selectors import select_jobs, validate_selector
from jobcrawler.rankers import rank_jobs
from jobcrawler.grouping import collapse_jobs
from jobcrawler.crawler import export_csv, save_cache
from jobcrawler.incremental import content_hash
from jobcrawler.personal import PersonalStore
from jobcrawler.inbox import refresh_intent, read_inbox, generate_digest


def job(ident="1", **values):
    return build_job(**{"id":ident, "source":"baidu", "company":"百度", "title":"后端工程师", "url":"https://talent.baidu.com/jobs/"+ident,
        "locations":["北京"], "requirements":"本科及以上学历，3年以上工作经验，熟悉 Python。" * 6,
        "description":"开发数据服务和公共基础设施，负责系统建设、代码维护和线上稳定性。" * 5, **values})


class MatchingTests(unittest.TestCase):
    def test_salary_keeps_units_currency_and_period_uncertainty(self):
        salary = parse_salary("月薪人民币 30-50k · 16薪")
        self.assertEqual((salary["min"],salary["max"],salary["currency"],salary["period"],salary["pay_months"]),(30000,50000,"CNY","month",16))
        self.assertIsNone(parse_salary("30-50K")["currency"])
        self.assertIsNone(parse_salary("30-50K")["period"])
        self.assertIsNone(parse_salary("面议")["min"])
        self.assertIsNone(parse_salary("薪资面议，3-5年经验")["min"])
        self.assertIsNone(parse_salary("50-30K")["min"])
        self.assertEqual(parse_salary({"currency":"USD","value":{"minValue":120000,"maxValue":150000,"unitText":"YEAR"}})["period"],"year")
        j=job(salary="年薪 USD 120000-150000")
        selected=select_jobs([j],{"salary_min":30000},include_uncertain=True)
        self.assertEqual(selected[0]["match_state"],"uncertain")
        self.assertEqual(select_jobs([j],{"salary_min":30000}),[])

    def test_fact_evidence_and_negations(self):
        j=job(requirements="本科及以上学历，硕士优先。不支持远程办公。",title="Database Internals Engineer")
        self.assertEqual(j["education"]["level"],"bachelor")
        self.assertEqual(j["education"]["preferred"],"master")
        self.assertEqual(j["office_mode"],"unknown")
        self.assertEqual(j["employment_type"],"unknown")
        self.assertEqual(job(description="开发远程办公系统和 hybrid cloud 产品")["office_mode"],"unknown")
        self.assertEqual(job(office_mode="远程办公")["office_mode"],"remote")
        self.assertEqual(len(select_jobs([job(education="学历不限")],{"education":"associate"})),1)
        self.assertEqual(normalize_fields(j),normalize_fields({**j,**normalize_fields(j)}))
        self.assertEqual(job(office_mode="TELECOMMUTE",employment_type="FULL_TIME")["office_mode"],"remote")
        self.assertEqual(job(education="硕士",requirements="")["education"]["level"],"master")
        self.assertIsNone(job(requirements="硕士优先")["education"]["level"])
        self.assertIsNone(job(requirements="本科及以上学历优先")["education"]["level"])
        self.assertEqual(select_jobs([j],{"education":"associate"},True),[])

    def test_existing_source_verifier_supports_relevance_without_preferences(self):
        from scripts.verify_sources import verify_pipeline
        with tempfile.TemporaryDirectory() as directory:
            jobs=[job("1",published_at="2026-10-21",work_years="1年"),job("2",published_at="2026-10-20",work_years="5年")]
            result=verify_pipeline({"source_id":"baidu","jobs":jobs,"complete":True},Path(directory))
            self.assertIn("relevance_desc",result["rankers_verified"])

    def test_preferences_never_exclude_and_unknown_never_adds_points(self):
        wanted={"locations":["北京"],"preferences":{"technologies":["Rust"],"office_modes":["remote"]}}
        selected=select_jobs([job()],wanted,True)
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0]["match_score"],0)
        self.assertEqual([c["status"] for c in selected[0]["preference_explanation"]],["unknown","unmet"])
        self.assertEqual(select_jobs([job()],{"technologies":["Rust"]},True),[])
        self.assertEqual(validate_selector(wanted)["preferences"],{"technologies":["Rust"],"technology_mode":"all","office_modes":["remote"]})
        a=job(office_mode="remote");b=job("2",office_mode="onsite",published_at="2026-10-27")
        ranked=rank_jobs(select_jobs([b,a],{"preferences":{"office_modes":["remote"]}}),"relevance_desc")
        self.assertEqual([j["id"] for j in ranked],["1","2"])
        self.assertIsNone(select_jobs([a],{})[0]["match_score"])
        ties=select_jobs([job("2"),job("1")], {"preferences":{"technologies":["Python"]}})
        self.assertEqual([j["id"] for j in rank_jobs(ties,"relevance_desc")],["1","2"])
        self.assertEqual([j["id"] for j in rank_jobs(list(reversed(ties)),"relevance_desc")],["1","2"])
        with self.assertRaises(ValueError):validate_selector({"preferences":{"exclude_keywords":["销售"]}})

    def test_unknown_hard_fields_only_relax_missing_evidence(self):
        unknown=job(requirements="",description="")
        self.assertEqual(select_jobs([unknown],{"office_modes":["remote"]}),[])
        self.assertEqual(select_jobs([unknown],{"office_modes":["remote"]},True)[0]["match_state"],"uncertain")
        self.assertEqual(select_jobs([job(office_mode="onsite")],{"office_modes":["remote"]},True),[])
        self.assertEqual(select_jobs([job(education="博士")],{"education":"bachelor"},True),[])

    def test_duplicate_groups_keep_members_and_csv_identity(self):
        first,second=job(),job("2")
        groups=collapse_jobs([first,second])
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]["duplicate_count"],2)
        members=groups[0]["duplicate_jobs"]
        self.assertEqual([j["id"] for j in members],["1","2"])
        rows=list(csv.DictReader(io.StringIO(export_csv(groups).lstrip('\ufeff'))))
        self.assertEqual([r["id"] for r in rows],["1","2"])
        self.assertEqual(rows[0]["duplicate_group"],rows[1]["duplicate_group"])
        self.assertIn("field_evidence",rows[0])
        for different in (job("2",company="其他公司"),job("2",locations=["上海"]),job("2",department="其他部门"),job("2",title="高级后端工程师"),job("2",requirements="完全不同的要求")):
            self.assertEqual(len(collapse_jobs([first,different])),2)
        self.assertEqual(len(collapse_jobs([job(office_mode="remote"),job("2",office_mode="onsite")])),2)
        self.assertEqual(len(collapse_jobs([job(company=""),job("2",company="")])),2)

    def test_canonical_content_ignores_presentation_and_handles_legacy(self):
        current=job();legacy={k:v for k,v in current.items() if k not in ("salary","education","office_mode","employment_type","field_evidence") and not k.endswith("_raw")}
        self.assertEqual(content_hash(current),content_hash(legacy))
        self.assertEqual(field_changes(current,{**current,"updated_at":"2026-10-27"}),[])
        self.assertNotEqual(content_hash(current),content_hash({**current,"updated_at":"2026-10-27"}))
        self.assertEqual(content_hash(current,ignore_metadata=True),content_hash({**current,"match_score":100,"updated_at":"2026-10-27"},ignore_metadata=True))
        self.assertEqual(field_changes(current,{**current,"description":current["description"]+'  \n'}),[])

    def test_field_changes_persist_without_losing_unread_version(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)/"jobs.json";store=PersonalStore(Path(directory)/"personal.sqlite3")
            def snapshot(item,date):save_cache({"source_id":"baidu","jobs":[item],"fetched_at":date,"collected":1,"complete":True},cache)
            first=job();snapshot(first,"2026-10-20")
            intent=store.save_intent({"name":"后端","sources":["baidu"],"selector":{"preferences":{"technologies":["Python"]}},"ranker":"relevance_desc"})
            self.assertEqual(refresh_intent(store,intent,cache)["baseline"],1)
            with store.connect() as db:db.execute("UPDATE inbox SET hash='legacy-hash'")
            self.assertEqual(refresh_intent(store,intent,cache)["updated"],0)
            updated=job(salary="月薪人民币 30-50k",office_mode="remote")
            snapshot(updated,"2026-10-21")
            self.assertEqual(refresh_intent(store,intent,cache)["updated"],1)
            restored=PersonalStore(store.path)
            item=read_inbox(restored,intent["id"])["jobs"][0]
            self.assertEqual((item["inbox_version"],item["read_state"]),(2,"unread"))
            fields={c["field"] for c in item["field_changes"]}
            self.assertEqual(fields,{"salary","office_mode"})
            store.digest_settings(intent["id"], {"enabled":True,"quiet_start":0,"quiet_end":0,"include_updates":True})
            digest=generate_digest(store,intent["id"],Path(directory)/"digests")
            self.assertEqual(digest["entries"][0]["field_changes"],item["field_changes"])
            self.assertEqual(next(c for c in item["field_changes"] if c["field"]=="office_mode")["before"],"unknown")
            snapshot({**updated,"updated_at":"2026-10-22"},"2026-10-22")
            self.assertEqual(refresh_intent(store,intent,cache)["updated"],0)
            self.assertEqual(read_inbox(store,intent["id"])["jobs"][0]["inbox_version"],2)
            store.mark_read(intent["id"],"baidu","1",1,True)
            self.assertEqual(read_inbox(store,intent["id"])["total"],1)

    def test_preference_edit_does_not_baseline_unseen_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)/"jobs.json";store=PersonalStore(Path(directory)/"personal.sqlite3")
            def snapshot(jobs,date):save_cache({"source_id":"baidu","jobs":jobs,"fetched_at":date,"collected":len(jobs),"complete":True},cache)
            snapshot([job()],"2026-10-20")
            intent=store.save_intent({"name":"后端","sources":["baidu"],"selector":{}})
            refresh_intent(store,intent,cache)
            snapshot([job(),job("2")],"2026-10-21")
            changed=store.save_intent({**intent,"selector":{"preferences":{"technologies":["Rust"]}},"collapse_duplicates":True})
            self.assertEqual(changed["baseline_sources"],[])
            changes=refresh_intent(store,changed,cache)
            self.assertEqual((changes["new"],changes["baseline"]),(1,0))
            self.assertEqual(read_inbox(store,intent["id"])["jobs"][0]["id"],"2")
            self.assertNotIn("previous_settings",changed)


    def test_baidu_explicit_pages_allow_duplicate_records_but_reject_missing_rows(self):
        from unittest.mock import patch
        from jobcrawler.sources.baidu import BaiduSource
        from jobcrawler.crawler import crawl
        def response(page,total=3,pages=2,rows=None):
            rows = rows if rows is not None else ([{"postId":"1","name":"后端"},{"postId":"2","name":"前端"}] if page==1 else [{"postId":"2","name":"前端"}])
            return io.StringIO(json.dumps({"status":"ok","data":{"pageNum":page,"total":str(total),"pages":pages,"pageSize":2,"hasNextPage":False,"list":rows}}))
        adapter=BaiduSource()
        with patch('jobcrawler.sources.baidu.urlopen',side_effect=[response(1),response(2)]), patch('jobcrawler.crawler.time.sleep'):
            result=crawl(source='baidu',fetch=adapter.fetch_page)
        self.assertTrue(result['complete'])
        self.assertEqual((result['scanned'],result['duplicates'],result['collected'],result['pages']),(3,1,2,2))
        with patch('jobcrawler.sources.baidu.urlopen',return_value=response(2,rows=[])):
            with self.assertRaisesRegex(RuntimeError,'条数'):adapter.fetch_page(2)
        with patch('jobcrawler.sources.baidu.urlopen',return_value=response(2,pages=3)):
            with self.assertRaisesRegex(RuntimeError,'元数据'):adapter.fetch_page(2)


class MatchingApiTests(unittest.TestCase):
    def test_saved_preferences_grouped_exports_and_application_filter(self):
        import threading
        from urllib.parse import urlencode
        from urllib.request import urlopen, Request
        from jobcrawler.server import make_server
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)/"jobs.json"
            snapshot={"source_id":"baidu","jobs":[job(),job("2"),job("3",locations=["上海"],office_mode="remote")],"fetched_at":"2026-10-27","collected":3,"complete":True}
            save_cache(snapshot,cache)
            server=make_server(port=0,cache=cache)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            def get(path,raw=False,**query):
                with urlopen(base+path+'?'+urlencode(query)) as response:
                    data=response.read()
                    return data.decode('utf-8-sig') if raw else json.loads(data)
            def post(path,body):
                with urlopen(Request(base+path,data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})) as response:return json.load(response)
            try:
                selector={"preferences":{"office_modes":["remote"]}}
                query={"sources":"baidu","ranker":"relevance_desc","selector":json.dumps(selector),"collapse_duplicates":1}
                result=get('/api/jobs',limit=1,**query)
                self.assertEqual((result["raw_total"],result["total"]),(3,2))
                self.assertEqual(result["jobs"][0]["id"],"3")
                exported=get('/api/export',limit=1,**query)
                self.assertEqual(exported["jobs"][1]["duplicate_count"],2)
                rows=list(csv.DictReader(io.StringIO(get('/api/export',raw=True,format='csv',**query))))
                self.assertEqual([r["id"] for r in rows],["3","1","2"])
                post('/api/applications',{"source":"baidu","id":"2","status":"applied"})
                applied=get('/api/export',application_status='applied',**query)
                self.assertEqual((applied["raw_total"],applied["total"]),(1,1))
                self.assertEqual(applied["jobs"][0]["id"],"2")
                intent=post('/api/intents',{"name":"远程偏好","sources":["baidu"],"selector":selector,"ranker":"relevance_desc","collapse_duplicates":True})
                inbox=get('/api/inbox/export',intent_id=intent["id"],state='all')
                self.assertEqual(inbox["intent"]["selector"]["preferences"],{"office_modes":["remote"]})
                self.assertEqual(inbox["total"],2)
                self.assertEqual(inbox["jobs"][0]["match_score"],100)
                # Display folding never merges saved identities or read versions.
                store=PersonalStore(cache.parent/'personal.sqlite3')
                with store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0],3)
                raw=get('/api/inbox/export',intent_id=intent["id"],state='all',collapse_duplicates=0)
                self.assertEqual(raw["total"],3)
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_jobposting_maps_explicit_fact_fields(self):
        from jobcrawler.sources.custom import CustomSource
        adapter=CustomSource({"id":"custom_facts","name":"Facts","url":"https://example.com/jobs","parser":"schema","mode":"document"})
        row={"@type":"JobPosting","identifier":"real-schema-test","title":"工程师","url":"https://example.com/jobs/1","description":"平台研发",
             "baseSalary":{"currency":"USD","value":{"minValue":120000,"maxValue":180000,"unitText":"YEAR"}},
             "educationRequirements":"bachelor","employmentType":"FULL_TIME","jobLocationType":"TELECOMMUTE"}
        result=adapter.parse_document('<script type="application/ld+json">'+json.dumps(row)+'</script>').jobs[0]
        self.assertEqual(result["salary"]["max"],180000)
        self.assertEqual(result["education"]["level"],"bachelor")
        self.assertEqual(result["office_mode"],"remote")
        self.assertEqual(result["employment_type"],"full_time")
