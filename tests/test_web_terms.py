import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'browser term parsing check requires node')
class WebTermsTests(unittest.TestCase):
    def test_unified_search_pagination_exports_and_stale_response_guard(self):
        html = (Path(__file__).resolve().parents[1] / 'jobcrawler/web/index.html').read_text()
        search = html[html.index('async function search('):html.index('function selectedSources(')]
        script = '''
const nodes={}; const $=id=>nodes[id]||(nodes[id]={value:'published_desc',checked:true,disabled:false});
const selector=()=>({years:3}), selectedSources=()=>['baidu','tencent'];
let jobs=[],searchVersion=0,active={},activeQuery='',searchTotal=0,hasMore=false;
const localStorage={setItem(){}}, error=message=>{if(message)throw Error(message)}, render=()=>{};
let slow=false,resolveSlow,queries=[];
function response(query){const offset=Number(query.get('offset')||0);return {jobs:Array.from({length:30},(_,i)=>({id:String(offset+i)})),total:60,has_more:offset===0,matched_count:60,uncertain_count:0,sources:['baidu','tencent'],snapshots:[],source_errors:[]};}
async function request(url){const query=new URLSearchParams(url.split('?')[1]);queries.push(query);if(slow)return await new Promise(resolve=>resolveSlow=resolve);return response(query);}
''' + search + '''
(async()=>{
 await search();if(jobs.length!==30||!hasMore)throw Error('first page');
 const exportQuery=new URLSearchParams($('csv-export').href.split('?')[1]);
 if(exportQuery.get('sources')!=='baidu,tencent'||exportQuery.has('limit')||exportQuery.has('offset'))throw Error('full export scope');
 await search(true);if(jobs.length!==60||hasMore||new Set(jobs.map(j=>j.id)).size!==60)throw Error('append');
 slow=true;const stale=search();slow=false;await search();const latest=JSON.stringify(jobs);
 resolveSlow({...response(new URLSearchParams()),jobs:[{id:'stale'}]});await stale;
 if(JSON.stringify(jobs)!==latest)throw Error('stale response replaced current search');
})().catch(error=>{console.error(error);process.exit(1)});
'''
        completed = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_recruitment_type_selector_restore_and_reset(self):
        html = (Path(__file__).resolve().parents[1] / 'jobcrawler/web/index.html').read_text()
        selector = html[html.index('function selector()'):html.index('function error(')]
        restore = html[html.index('function restore(s)'):html.index('async function status(')]
        functions = html[html.index('function splitTerms'):html.index('const esc =')]
        script = '''const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:false});
''' + functions + selector + restore + '''
for(const kind of ['all','campus','social','unknown']){
 restore({recruitment_type:kind});
 const saved=JSON.parse(JSON.stringify(selector()));
 if(saved.recruitment_type!==kind)throw Error('select '+kind);
 restore(saved);
 if($('recruitment-type').value!==kind)throw Error('restore '+kind);
}
restore({});if(selector().recruitment_type!=='all')throw Error('reset/legacy');
'''
        completed = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_quoted_conditions_and_restore_roundtrip(self):
        html = (Path(__file__).resolve().parents[1] / 'jobcrawler/web/index.html').read_text()
        functions = html[html.index('function splitTerms'):html.index('const esc =')]
        cases = [
            ('北京，上海、深圳\n杭州', ['北京', '上海', '深圳', '杭州']),
            ('"Foster City, CA", "Boston, MA"', ['Foster City, CA', 'Boston, MA']),
            ('"Customer Education Program Manager, Live Learning"', ['Customer Education Program Manager, Live Learning']),
            ('"Quote ""inside"", CA",北京', ['Quote "inside", CA', '北京']),
            ('  Python, ,Java，Go ', ['Python', 'Java', 'Go']),
        ]
        script = functions + '\nconst cases=' + json.dumps(cases) + ''';
for(const [input,expected] of cases){
 if(JSON.stringify(splitTerms(input))!==JSON.stringify(expected))throw Error(input);
 if(JSON.stringify(splitTerms(joinTerms(expected)))!==JSON.stringify(expected))throw Error('restore '+input);
}
'''
        completed = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
