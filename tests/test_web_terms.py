import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'browser term parsing check requires node')
class WebTermsTests(unittest.TestCase):
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
