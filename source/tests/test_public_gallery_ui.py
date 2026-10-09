"""Unit contracts for the actual public bootstrap; not browser/performance evidence."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function_source(name, asynchronous=False):
    source = (ROOT / 'web/public-gallery.js').read_text()
    marker = '  ' + ('async ' if asynchronous else '') + 'function ' + name + '('
    start = source.index(marker)
    end = source.index('\n  }', start)
    return source[start:end + 4]


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for public bootstrap units')
class PublicGalleryUITests(unittest.TestCase):
    def javascript(self, functions, body):
        program = "const assert=require('node:assert/strict');\n(async()=>{\n" + '\n'.join(
            function_source(name, asynchronous) for name, asynchronous in functions)
        program += '\n' + body + '\n})().catch(error=>{console.error(error);process.exitCode=1});'
        result = subprocess.run(['node', '-e', program], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_viewer_paths_cannot_escape_the_same_directory(self):
        self.javascript([('pathURL', False)], """
global.location={href:'https://example.test/report/?purpose=benchmark#gallery',origin:'https://example.test'};
const name='viewer/'+'a'.repeat(64)+'.html';
assert.equal(pathURL(name,'html'),'https://example.test/report/'+name);
for(const path of ['../'+name,'/'+name,'https://evil.test/'+name,name+'?extra=1',name.replace('.html','.svg')]) assert.throws(()=>pathURL(path,'html'));
""")

    def test_legacy_queries_request_the_full_viewer_without_losing_empty_values(self):
        self.javascript([('readURL', False)], """
const seed={defaults:{task_id:'crocodile',purpose:'benchmark'}};
const state={};
for(const [query,hash,advanced,task,purpose] of [
 ['', '', false,'crocodile','benchmark'],
 ['?purpose=&task_id=', '#gallery', false,'',''],
 ['?purpose=benchmark', '', false,'','benchmark'],
 ['?task_id=unknown&purpose=smoke', '', false,'unknown','smoke'],
 ['?q=arbitrary', '#gallery', true,'','benchmark'],
 ['', '#matrix', true,'crocodile','benchmark']]) {
 global.location={search:query,hash};
 assert.equal(readURL(),advanced);assert.equal(state.task,task);assert.equal(state.purpose,purpose);
}
""")

    def test_light_model_search_dates_and_unknowns_are_not_scores(self):
        self.javascript([('matches', False)], """
const text=value=>value==null||value===''?'unknown':String(value);
const state={task:'crocodile',purpose:'benchmark',model:' GPT ',from:'2026-10-08',to:'2026-10-08'};
const run={model:'gpt-6',task_id:'crocodile',purpose:'benchmark',date:'2026-10-08'};
assert.equal(matches(run),true);
assert.equal(matches({...run,date:null}),false);
assert.equal(matches({...run,purpose:'smoke'}),false);
state.model='review-only';assert.equal(matches({...run,reviews:[{text:'review-only'}]}),false);
state.model='';state.from='';state.to='';assert.equal(matches({...run,date:null}),true);
""")

    def test_abort_does_not_clear_a_newer_single_flight(self):
        self.javascript([('mountFull', True)], """
let fullPromise=null,fullFetch=null,fatal=false;
const calls=[];
const readFull=signal=>new Promise((resolve,reject)=>calls.push({signal,resolve,reject}));
const first=mountFull();assert.equal(calls.length,1);
const shared=mountFull();assert.equal(calls.length,1);
fullFetch.abort();
const second=mountFull();assert.equal(calls.length,2);
const newest=fullPromise;
calls[0].reject(new DOMException('cancelled','AbortError'));
await assert.rejects(first,/cancelled/);await assert.rejects(shared,/cancelled/);
assert.equal(fullPromise,newest);
fullFetch.abort();calls[1].resolve({});await assert.rejects(second,/cancelled/);
assert.equal(fullPromise,null);
""")

    def test_decoded_stream_bounds_hash_mime_and_gzip_lengths(self):
        self.javascript([('readFull', True)], """
const crypto=require('node:crypto').webcrypto;
const digest=async bytes=>Buffer.from(await crypto.subtle.digest('SHA-256',bytes)).toString('hex');
const prefix='window.BENCH_SNAPSHOT=';
const data={format:'static-media-v2',transport:'external',runs:[{id:'r1'}]};
const program='trusted-test-viewer';
const payload=new TextEncoder().encode('synthetic complete document');
const seed={counts:{runs:1},full:{path:'viewer/'+'a'.repeat(64)+'.html',size:payload.length,sha256:await digest(payload),script_sha256:await digest(new TextEncoder().encode(program))}};
const records=new Map([['r1',{}]]);
const pathURL=()=> 'https://example.test/report/viewer/a.html';
class DOMParser {parseFromString(){const scripts=[{textContent:prefix+JSON.stringify(data)+';',hasAttribute:()=>false,remove(){}},{textContent:program,hasAttribute:()=>false,remove(){}}];return {querySelectorAll:()=>scripts,querySelector:()=>null};}}
let body=payload,headers={};
global.fetch=async()=>({ok:true,redirected:false,headers:new Headers({'Content-Type':'text/html',...headers}),body:new ReadableStream({start(controller){controller.enqueue(body);controller.close();}})});
headers={'Content-Encoding':'gzip','Content-Length':'1'};
assert.equal((await readFull(new AbortController().signal)).data.runs[0].id,'r1');
headers={'Content-Length':'1'};await assert.rejects(readFull(new AbortController().signal),/长度/);
headers={'Content-Encoding':'gzip','Content-Length':'not-numeric'};await assert.rejects(readFull(new AbortController().signal),/长度/);
headers={'Content-Encoding':'gzip'};body=new Uint8Array(payload.length+1);await assert.rejects(readFull(new AbortController().signal),/超限/);
body=payload.slice(1);await assert.rejects(readFull(new AbortController().signal),/不完整/);
body=new Uint8Array(payload.length);await assert.rejects(readFull(new AbortController().signal),/SHA256/);
body=payload;headers={'Content-Type':'application/javascript'};await assert.rejects(readFull(new AbortController().signal),/响应/);
headers={};seed.full.script_sha256='0'.repeat(64);await assert.rejects(readFull(new AbortController().signal),/代码摘要/);
""")


if __name__ == '__main__':
    unittest.main()
