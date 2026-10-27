"""Unit checks for asynchronous page handlers; these do not operate a browser."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "JavaScript unit checks require Node")
class WebWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "jobcrawler/web/index.html").read_text()

    def section(self, start, end):
        return self.html[self.html.index(start):self.html.index(end)]

    def run_script(self, code):
        result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_matching_and_field_comparisons_render_readable_values_and_escape_text(self):
        self.run_script(self.section("const esc =", "function trackingControls(") +
            self.section("function jobValue(", "async function search(") + """
const job={salary:{raw:''},education:{level:'bachelor'},office_mode:'hybrid',employment_type:'full_time',match_score:50,
 preference_explanation:[{condition:'office_modes',label:'办公方式',requested:['remote','hybrid'],status:'matched',evidence:'混合办公'}],
 field_evidence:{office_mode:{text:'混合办公',basis:'source'}},
 field_changes:[{field:'office_mode',label:'办公方式',before:'onsite',after:'hybrid'},
  {field:'education',label:'学历',before:{level:'bachelor'},after:{level:'master',preferred:'doctorate'}},
  {field:'salary',label:'薪资',before:{raw:'面议'},after:{raw:'{"currency":"CNY"}',min:30000,max:50000,currency:'CNY',period:'month'}},
  {field:'requirements',label:'任职要求',before:'<img src=x onerror=alert(1)>',after:'公开要求'}],inbox_version:2};
const html=jobFacts(job);
for(const text of ['远程办公 / 混合办公','现场办公','最低要求：硕士','优先：博士','CNY 30000–50000 / 月','字段变化 · v2'])if(!html.includes(text))throw Error('missing readable value '+text);
if(html.includes('"level"')||html.includes('<img'))throw Error('raw JSON or unescaped posting content');
if(!html.includes('&lt;img'))throw Error('original posting evidence lost');
if(jobValue('read_state','unread')!=='未读'||jobValue('years',3)!=='3 年')throw Error('untranslated state or years');
""")

    def test_digest_response_stays_with_original_intent_and_blank_hours_rejected(self):
        self.run_script('''
const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:false,textContent:''});
let intents=[{id:'a',digest:{enabled:false}},{id:'b',digest:{enabled:false}}],resolve,body,calls=0,messages=[];
const error=message=>messages.push(message);
async function request(url,options){calls++;body=JSON.parse(options.body);return await new Promise(r=>resolve=r);}
''' + self.section("async function saveDigest()", "$('save-digest').onclick=") + '''
(async()=>{
 $('intent-list').value='a';$('quiet-start').value='22';$('quiet-end').value='8';
 $('digest-enabled').checked=true;$('digest-frequency').value='daily';
 const saving=saveDigest();if(body.intent_id!=='a')throw Error('wrong request identity');
 $('intent-list').value='b';$('digest-message').textContent='B settings';
 resolve({enabled:true,frequency:'daily'});await saving;
 if(!intents[0].digest.enabled||intents[1].digest.enabled)throw Error('response changed another intention');
 if($('digest-message').textContent!=='B settings')throw Error('response changed another intention message');
 $('quiet-start').value='';await saveDigest();
 if(calls!==1||!messages.length)throw Error('blank quiet hour silently became zero');
 $('intent-list').value='';await saveDigest();if(calls!==1)throw Error('unsaved intention submitted');
})().catch(error=>{console.error(error);process.exit(1)});
''')

    def test_application_response_does_not_mark_a_replacement_card_saved(self):
        self.run_script('''
const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',textContent:''});
let jobs=[{source:'baidu',id:'old'}],searchVersion=1,resolve,body;
const applicationLabels={applied:'已投递'},error=message=>{throw Error(message)};
const summary={textContent:''},button={dataset:{index:'0'},disabled:false,classList:{contains:name=>name==='application-save'},closest:()=>({querySelector:()=>summary})};
const event={target:{closest:()=>button}};
async function request(url,options){body=JSON.parse(options.body);return await new Promise(r=>resolve=r);}
''' + self.section("$('results').onclick=", "async function loadNotifications()") + '''
(async()=>{
 $('application-0').value='applied';$('notes-0').value='old job note';
 const saving=$('results').onclick(event);if(body.id!=='old')throw Error('wrong job identity');
 jobs=[{source:'tencent',id:'new'}];searchVersion++;
 $('application-message-0').textContent='new card';resolve({});await saving;
 if($('application-message-0').textContent!=='new card'||summary.textContent)throw Error('stale save changed replacement card');
 const fresh=$('results').onclick(event);resolve({});await fresh;
 if(summary.textContent!=='投递跟进：已投递'||$('application-message-0').textContent!==' 已保存')throw Error('current save failed to update label');
 if(button.disabled)throw Error('save button remains disabled');
})().catch(error=>{console.error(error);process.exit(1)});
''')

    def test_repeated_intent_save_has_one_request_and_recovers_after_failure(self):
        self.run_script('''
const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:false,disabled:false,textContent:''});
const selectedSources=()=>['baidu'],selector=()=>({});
let calls=0,resolve,reject,loaded=0,errors=[];
const loadIntents=async()=>loaded++,search=async()=>{},error=message=>errors.push(message);
async function request(){calls++;return await new Promise((a,b)=>{resolve=a;reject=b;});}
''' + self.section("async function saveIntent(", "$('save-intent').onclick=") + '''
(async()=>{
 $('intent-name').value='test';const first=saveIntent();await saveIntent(true);
 if(calls!==1||!$('save-intent').disabled||!$('new-intent').disabled)throw Error('duplicate intent request');
 resolve({id:'a'});await first;if(loaded!==1||$('save-intent').disabled)throw Error('save did not recover');
 const failed=saveIntent();reject(Error('disk failure'));await failed;
 if($('save-intent').disabled||$('new-intent').disabled||errors[0]!=='disk failure')throw Error('failure disabled future saves');
})().catch(error=>{console.error(error);process.exit(1)});
''')

    def test_empty_inbox_explains_baseline_and_tracking_explains_manual_records(self):
        self.run_script('''
const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:true});
let jobs=[],searchTotal=0,hasMore=false;
''' + self.section("function render()", "async function search(") + '''
$('workspace-view').value='inbox';$('intent-list').value='saved';render();
if(!$('results').innerHTML.includes('已读基线')||$('results').innerHTML.includes('放宽筛选'))throw Error('misleading inbox empty state');
$('intent-list').value='';render();if(!$('results').innerHTML.includes('请先选择或保存'))throw Error('unsaved intent empty state');
$('workspace-view').value='applications';render();if(!$('results').innerHTML.includes('投递跟进记录'))throw Error('tracking empty state');
''')

    def test_edit_cancel_is_read_only_and_confirm_uses_preview_revision(self):
        self.run_script('''
const nodes={};const $=id=>nodes[id]||(nodes[id]={value:'',checked:false,disabled:false,textContent:''});
const selectedSources=()=>['baidu'],selector=()=>({keywords:['Python']});
let bodies=[],messages=[];
const loadIntents=async()=>{},search=async()=>{},error=message=>messages.push(message);
async function request(path,options){bodies.push([path,JSON.parse(options.body)]);return path.endsWith('/preview')?{expected_revision:7}:{id:'saved'};}
''' + self.section("async function saveIntent(", "$('save-intent').onclick=") + '''
(async()=>{
 $('intent-list').value='saved';$('intent-name').value='Python';
 confirmIntent=async()=>null;await saveIntent();
 if(bodies.length!==1||bodies[0][0]!=='/api/intents/preview')throw Error('cancel mutated intention');
 bodies=[];confirmIntent=async()=> 'preserve';await saveIntent();
 const saved=bodies[1][1];if(saved.expected_revision!==7||saved.edit_mode!=='preserve'||saved.restart_confirmed)throw Error('unsafe edit confirmation');
 if(messages.length)throw Error(messages.join(';'));
})().catch(error=>{console.error(error);process.exit(1)});
''')

    def test_home_uses_existing_and_unread_entry_points_and_ignores_stale_response(self):
        self.run_script('''
const nodes={};const $=id=>nodes[id]||(nodes[id]={textContent:'',innerHTML:''});
const esc=String,applicationLabels={planned:'准备投递',applied:'已投递',interview:'面试中'};
let homeVersion=0,resolve,slow=true;
function response(name){return {intents:[{id:'profile',name,unread:2,updated:1,matching:10,uncertain:3,source_errors:[]}],followup:{planned:1,applied:2,interview:0},favorites:1,unread_notifications:0};}
async function request(){if(slow)return await new Promise(r=>resolve=r);return response('latest');}
''' + self.section("async function loadHome()", "$('home-content').onclick=") + '''
(async()=>{
 const old=loadHome();slow=false;await loadHome();resolve(response('old'));await old;
 const html=$('home-content').innerHTML;
 if(!html.includes('latest')||html.includes('old')||!html.includes('data-state="all"')||!html.includes('data-state="unread"')||!html.includes('data-stage="planned"'))throw Error('home summary or race guard');
})().catch(error=>{console.error(error);process.exit(1)});
''')

    def test_health_banner_distinguishes_failed_refresh_from_success_with_zero_new(self):
        self.run_script('''
const nodes={};const $=id=>nodes[id]||(nodes[id]={textContent:'',innerHTML:''});const esc=String;
let succeeded=false;
async function request(){return {latest_run:{outcome:succeeded?'refreshed':'no_fresh_data',successful:succeeded?1:0,total:1,failed:succeeded?0:1,new_count:succeeded?0:null},warnings:[],sources:[{source:'baidu',name:'百度',status:succeeded?'ok':'error',mode:'http',schedulable:true,collected:99,complete:true,scope:'listing',new_count:succeeded?0:null,error:succeeded?null:'network failure',action:'刷新来源'}]};}
''' + self.section("async function loadHealth()", "$('health-items').onclick=") + '''
(async()=>{
 await loadHealth();if(!$('health-banner').textContent.includes('不能判断没有新岗位')||$('health-items').innerHTML.includes('本次新增 0'))throw Error('failure presented as zero new');
 succeeded=true;await loadHealth();if(!$('health-banner').textContent.includes('新增 0 条')||!$('health-items').innerHTML.includes('本次新增 0 条'))throw Error('genuine zero new was hidden');
})().catch(error=>{console.error(error);process.exit(1)});
''')
