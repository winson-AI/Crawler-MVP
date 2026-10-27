import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'browser term parsing check requires node')
class WebTermsTests(unittest.TestCase):
    def test_complete_script_parses_and_inbox_exports_keep_saved_scope(self):
        html = (Path(__file__).resolve().parents[1] / 'jobcrawler/web/index.html').read_text()
        whole = html.split('<script>', 1)[1].split('</script>', 1)[0]
        parsed = subprocess.run(['node', '-e', 'new Function(' + json.dumps(whole) + ')'], capture_output=True, text=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        search = html[html.index('async function search('):html.index('function selectedSources(')]
        script = '''
const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:false,removeAttribute(){}});
const selector=()=>({keywords:['unsaved']}), selectedSources=()=>['baidu'];
let jobs=[],searchVersion=0,active={},activeQuery='',searchTotal=0,hasMore=false;
const localStorage={setItem(){}},error=message=>{if(message)throw Error(message)},render=()=>{};
let requested;
async function request(url){requested=url;return url.startsWith('/api/inbox?')?{jobs:[],total:0,has_more:false,intent:{name:'saved'},counts:{unread:0,read:2,updated:1},source_errors:[]}:{jobs:[],total:0,has_more:false,matched_count:0,uncertain_count:0,sources:['baidu'],snapshots:[],source_errors:[]};}
''' + search + '''
(async()=>{
 $('workspace-view').value='inbox';$('intent-list').value='saved-id';$('inbox-filter').value='updated';await search();
 const q=new URLSearchParams(requested.split('?')[1]);
 if(!requested.startsWith('/api/inbox?')||q.get('intent_id')!=='saved-id'||q.get('state')!=='updated'||q.has('selector'))throw Error('saved inbox scope');
 if(!$('csv-export').href.startsWith('/api/inbox/export?')||$('csv-export').href.includes('limit='))throw Error('inbox export scope');
 $('workspace-view').value='applications';await search();
 if(new URLSearchParams(requested.split('?')[1]).get('applications')!=='1'||!$('csv-export').href.includes('applications=1'))throw Error('application export scope');
})().catch(error=>{console.error(error);process.exit(1)});
'''
        completed = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_unified_search_pagination_exports_and_stale_response_guard(self):
        html = (Path(__file__).resolve().parents[1] / 'jobcrawler/web/index.html').read_text()
        search = html[html.index('async function search('):html.index('function selectedSources(')]
        script = '''
const nodes={}; const $=id=>nodes[id]||(nodes[id]={value:'published_desc',checked:true,disabled:false,removeAttribute(){}});
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
        script = '''const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:false,querySelectorAll:()=>[]});
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

    def test_hard_and_preference_restore_does_not_move_other_conditions(self):
        html = (Path(__file__).resolve().parents[1] / 'jobcrawler/web/index.html').read_text()
        code = html[html.index('function splitTerms'):html.index('const esc =')]
        code += html[html.index('function selector()'):html.index('function error(')]
        code += html[html.index('function restore(s)'):html.index('async function status(')]
        script = "const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:false,querySelectorAll:()=>[]});" + code + """
const saved={locations:['北京'],exclude_keywords:['销售'],preferences:{technologies:['Python'],technology_mode:'all',salary_min:30000,salary_currency:'CNY',salary_period:'month'}};
restore(saved);const value=selector();
if(JSON.stringify(value.locations)!=='["北京"]'||value.technologies||value.salary_min!==undefined)throw Error('conditions moved or duplicated');
if(value.preferences.technologies[0]!=='Python'||value.preferences.salary_min!==30000||value.exclude_keywords[0]!=='销售')throw Error('preference/exclusions lost');
restore(value);if($('locations-strength').value!=='hard'||$('tech-strength').value!=='prefer')throw Error('strength lost');
restore({});if(Object.keys(selector().preferences).length)throw Error('reset kept preferences');
"""
        completed = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_multiple_office_and_employment_values_survive_restore_and_reset(self):
        html = (Path(__file__).resolve().parents[1] / 'jobcrawler/web/index.html').read_text()
        self.assertIn('<fieldset id="office-modes">', html)
        self.assertIn('<fieldset id="employment-types">', html)
        code = html[html.index('function splitTerms'):html.index('const esc =')]
        code += html[html.index('function selector()'):html.index('function error(')]
        code += html[html.index('function restore(s)'):html.index('async function status(')]
        script = """
const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:false,querySelectorAll:()=>[]});
for(const [id,values] of [['office-modes',['onsite','hybrid','remote']],['employment-types',['full_time','part_time','intern','contract']]]){
 const inputs=values.map(value=>({value,checked:false}));
 nodes[id]={querySelectorAll:selector=>selector==='input:checked'?inputs.filter(input=>input.checked):inputs};
}
""" + code + """
restore({office_modes:['remote','hybrid'],employment_types:['full_time','intern']});
let value=selector();
if(value.office_modes.join()!=='hybrid,remote'||value.employment_types.join()!=='full_time,intern')throw Error('hard multi-value conditions lost');
restore({preferences:{office_modes:['remote','hybrid'],employment_types:['full_time','intern']}});
value=selector();
if(value.office_modes!==undefined||value.preferences.office_modes.join()!=='hybrid,remote'||value.preferences.employment_types.join()!=='full_time,intern')throw Error('preferences moved or lost');
restore({});value=selector();
if(value.office_modes.length||value.employment_types.length||Object.keys(value.preferences).length)throw Error('reset kept old conditions');
"""
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
