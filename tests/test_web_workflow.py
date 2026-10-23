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
