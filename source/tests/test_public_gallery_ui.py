"""Unit contracts for the actual public bootstrap; not browser/performance evidence."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function_source(name, asynchronous=False):
    source = (ROOT / 'web/public-gallery.js').read_text()
    marker = '  ' + ('async ' if asynchronous else '') + 'function ' + name + '('
    start = source.index(marker)
    # Stop at the next top-level statement, not a formatted closing brace.
    # This keeps inline functions isolated without including adjacent constants.
    boundary = re.search(r'\n(?=  [^\s}])', source[start + len(marker):])
    end = start + len(marker) + boundary.start() if boundary else len(source)
    return source[start:end].rstrip()


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for public bootstrap units')
class PublicGalleryUITests(unittest.TestCase):
    def javascript(self, functions, body):
        program = "const assert=require('node:assert/strict');\n(async()=>{\n" + '\n'.join(
            function_source(name, asynchronous) for name, asynchronous in functions)
        program += '\n' + body + '\n})().catch(error=>{console.error(error);process.exitCode=1});'
        result = subprocess.run(['node', '-e', program], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def preview_javascript(self, body, extra_functions=()):
        functions = [('repairFor', False), ('previewChoice', False), ('previewLabel', False),
                     ('previewAvailable', False), ('previewFallback', False), ('setPreviewMode', False),
                     ('previewButtons', False), ('togglePreview', False),
                     ('stopPreview', False), ('destroyPreview', False),
                     ('suspendCards', False), ('clearCards', False),
                     ('resumeCards', False), ('pathURL', False), ('startPreview', True)]
        self.javascript(functions + list(extra_functions), """
let activePreview=null,previewObserver=null,fullPending=false,fullIntent=0,returnURL=null,fullFetch=null;
const cardPreviews=new Map(),seed={},s=/^[a-f0-9]{32}$/;
const allowedOriginal=run=>['ready','missing_dependencies'].includes(run.original.status);
const dialogs={'#original-dialog':{open:false},'#public-zoom':{open:false}};
const $=selector=>dialogs[selector]||null;
global.document={hidden:false};
global.location={href:'https://example.test/report/',origin:'https://example.test'};
const nativeFetch=global.fetch;
const created=[],started=[],destroyed=[],downloads=[],reads=[];
const response=()=>new Response(new Uint8Array([7,8,9]));
global.fetch=async(url,options)=>{downloads.push({url,signal:options.signal});assert.equal(options.cache,'no-store');assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');assert.equal(options.referrerPolicy,'no-referrer');if(options.signal.aborted)throw new DOMException('cancelled','AbortError');return response();};
const sdk={async readPackage(descriptor,value){reads.push(value);return new Uint8Array(await value.arrayBuffer());},create(host,options){
 let alive=true;
 const runtime={async start(bytes){assert.deepEqual(await bytes,new Uint8Array([7,8,9]));if(alive){started.push(options.runId);options.onState({status:'loaded'});}},destroy(){alive=false;destroyed.push(options.runId);}};
 created.push({host,options,runtime});return runtime;
}};
let sdkReady=Promise.resolve(sdk);
const ensureSDK=()=>sdkReady;
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function makePreview(id,paused=false){
 const preview={run:{id,original:{status:'ready',package:{path:'originals/'+'a'.repeat(64)+'.json',size:3,sha256:'a'.repeat(64)}}},node:{dataset:{previewState:'stopped'},isConnected:true},host:{style:{visibility:'hidden'},children:[],clears:0,replaceChildren(...children){this.children=children;this.clears++;}},status:{textContent:''},start:{disabled:false,textContent:'运行原作'},viewport:{width:1440,height:900},token:0,runtime:null,alive:true,inline:true,visible:true,paused};
 cardPreviews.set(id,preview);return preview;
}
""" + body)

    def repair_preview_javascript(self, body, extra_functions=()):
        self.preview_javascript(r"""
function makeRepair(id='1'.repeat(32),originalStatus='ready'){
 const preview=makePreview(id);
 Object.assign(preview.run,{model:'Synthetic model',image:{full:'media/'+'d'.repeat(64)+'.png',width:800,height:500,label:'历史截图'}});
 Object.assign(preview.run.original,{status:originalStatus,entrypoint:'index.html',entry_sha256:'a'.repeat(64),missing:[],policy:'opaque-srcdoc-v1'});
 if(originalStatus==='unsupported')preview.run.original.package=null;
 const descriptor={status:'ready',entrypoint:'index.html',entry_sha256:'b'.repeat(64),missing:[],policy:'opaque-srcdoc-v1',package:{path:'originals/'+'c'.repeat(64)+'.json',size:3,sha256:'c'.repeat(64)}};
 const repair={version:1,recipe_id:'synthetic-recipe-v1',source_sha256:'a'.repeat(64),operations:[{kind:'replace',before:'broken',after:'repaired'}],result_sha256:'b'.repeat(64),review:{reason:'固定修复说明 <script>not executable</script>'},descriptor};
 seed.repairs??={};seed.repairs[id]=repair;
 Object.assign(preview,previewChoice(preview.run));preview.host.dataset={};
 const buttons=['repair','original','static'].map(mode=>({dataset:{previewMode:mode},setAttribute(key,value){this[key]=value;}}));
 preview.controls={buttons,querySelectorAll:()=>buttons};preview.zoom={};
 return preview;
}
""" + body, extra_functions)

    def runtime_javascript(self, body):
        # Execute only the trusted SDK; fake srcdoc elements never execute originals.
        program = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
global.crypto=require('node:crypto').webcrypto;global.window=global;
const listeners=new Map(),observers=[],channels=[];
global.addEventListener=(type,listener)=>{if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(listener);};
global.removeEventListener=(type,listener)=>listeners.get(type)?.delete(listener);
class Element {
 constructor(tag='div'){this.tag=tag;this.style={};this.children=[];this.clientWidth=800;this.clientHeight=450;this.attributes={};}
 setAttribute(key,value){this.attributes[key]=value;}
 append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null;}
}
global.Element=Element;
global.document={baseURI:'https://example.test/gallery/',createElement:tag=>new Element(tag),addEventListener,removeEventListener};
global.ResizeObserver=class {constructor(callback){this.callback=callback;this.disconnected=0;observers.push(this);}observe(){}disconnect(){this.disconnected++;}};
global.MessageChannel=class {constructor(){this.port1={closed:0,close(){this.closed++;}};this.port2={closed:0,close(){this.closed++;}};channels.push(this);}};
let svgParses=0;
// Minimal XML tree for controlled SVG fixtures, not a browser/XML parser substitute.
global.DOMParser=class {parseFromString(text,mime){
 assert.equal(mime,'image/svg+xml');svgParses++;
 const root={nodeType:1,namespaceURI:'http://www.w3.org/2000/svg',localName:'svg',attributes:[],childNodes:[],children:[]};
 for(const match of text.matchAll(/\b(href|style)="([^"]*)"/g))root.attributes.push({localName:match[1],namespaceURI:null,value:match[2]});
 if(text.includes('<script'))root.childNodes.push({...root,localName:'script',attributes:[],childNodes:[]});
 return {documentElement:root,childNodes:[root],getElementsByTagName:()=>[]};
}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'),{filename:process.argv[1]});
(async()=>{
const hash=async bytes=>Buffer.from(await crypto.subtle.digest('SHA-256',bytes)).toString('hex');
const html='<!doctype html><html><head></head><body>synthetic original</body></html>';
async function fixture(change=()=>{},isSVG=false){
 const source=isSVG?'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>':html;
 const raw=new TextEncoder().encode(source),entrypoint=isSVG?'original.svg':'index.html';
 const pack={version:1,run_id:'synthetic-run',entrypoint,files:{[entrypoint]:{mime:isSVG?'image/svg+xml':'text/html',size:raw.length,sha256:await hash(raw),base64:Buffer.from(raw).toString('base64')}},adaptation:isSVG?{version:1,protocol:'standalone-svg-v1'}:{version:1,head_offset:html.indexOf('<head>')+6,patches:[]},missing:[]};
 await change(pack);
 const bytes=new TextEncoder().encode(JSON.stringify(pack)),sha256=await hash(bytes);
 const descriptor={status:'ready',entrypoint,entry_sha256:await hash(raw),missing:[],policy:'opaque-srcdoc-v1',package:{path:'originals/'+sha256+'.json',size:bytes.length,sha256}};
 return {pack,bytes,descriptor};
}
const nativeFetch=global.fetch;
async function packageServer(bytes){
 const http=require('node:http'),zlib=require('node:zlib');
 const server=http.createServer((request,response)=>{
  server.requests++;
  if(server.redirect){response.writeHead(302,{Location:server.target});response.end();return;}
  const body=server.bytes,wire=server.gzip?zlib.gzipSync(body):body;
  response.writeHead(server.status,{'Content-Type':server.mime,'Content-Length':server.headerLength??wire.length,...(server.gzip?{'Content-Encoding':'gzip'}:{})});
  if(server.hold){response.write(wire.subarray(0,1));server.response=response;}else response.end(wire);
 });
 Object.assign(server,{bytes,status:200,mime:'application/json',gzip:false,hold:false,redirect:false,requests:0});
 await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
 document.baseURI='http://127.0.0.1:'+server.address().port+'/gallery/';
 server.closeNow=()=>{server.closeAllConnections();return new Promise(resolve=>server.close(resolve));};
 return server;
}
function trackReader(response){
 const body=response.body,getReader=body.getReader.bind(body),cleanup={cancelled:0,released:0};
 body.getReader=()=>{const reader=getReader();return {read:()=>reader.read(),cancel:async()=>{cleanup.cancelled++;await reader.cancel();},releaseLock(){cleanup.released++;reader.releaseLock();}};};
 return cleanup;
}
let fetches=0;
global.fetch=async()=>{fetches++;throw new Error('supplied bytes must not fetch');};
function instance(descriptor,onState=()=>{}){const host=new Element(),states=[];const runtime=BenchPreview.create(host,{runId:'synthetic-run',descriptor,transport:'external',onState:value=>{states.push(value.status);onState(value);}});return {host,states,runtime};}
""" + body + r"""
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(['node', '-e', program, str(ROOT / 'web/preview-runtime.js')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repair_mapping_is_optional_bound_to_run_entry_and_strict_external_descriptor(self):
        self.repair_preview_javascript(r"""
const preview=makeRepair(),run=preview.run,valid=structuredClone(seed.repairs),id=run.id;
assert.equal(repairFor(run),seed.repairs[id]);assert.equal(previewChoice(run).mode,'repair');
const original=JSON.stringify(run.original),changes=[
 value=>{value[id].version=2;},value=>{value[id].source_sha256='0'.repeat(64);},
 value=>{value[id].result_sha256='0'.repeat(64);},value=>{value[id].recipe_id=' ';},
 value=>{value[id].operations=[];},value=>{value[id].review.reason=' ';},
 value=>{value[id].descriptor.status='unsupported';},value=>{value[id].descriptor.entrypoint='../index.html';},
 value=>{value[id].descriptor.policy='unsafe';},value=>{value[id].descriptor.extra=true;},
 value=>{value[id].descriptor.missing=['../secret'];},value=>{value[id].descriptor.missing=['asset.png'];},
 value=>{value[id].descriptor.package.path='https://evil.test/repair.json';},
 value=>{value[id].descriptor.package.path='originals/'+'0'.repeat(64)+'.json';},
 value=>{value[id].descriptor.package.path='../'+value[id].descriptor.package.path;},
 value=>{value[id].descriptor.package.size=4194305;},value=>{value[id].descriptor.package.size=0;},
 value=>{value[id].descriptor.package.base64='abc';},value=>{value[id].descriptor.entry_sha256=null;},
 value=>{value['2'.repeat(32)]=value[id];delete value[id];}
];
for(const change of changes){seed.repairs=structuredClone(valid);change(seed.repairs);assert.equal(repairFor(run),null);assert.equal(previewChoice(run).mode,'original');}
for(const value of [undefined,null,[],{},'not a map',Object.create(valid)]){seed.repairs=value;assert.equal(repairFor(run),null);}
seed.repairs=valid;assert.equal(repairFor({...run,id:'../'+id}),null);
assert.equal(repairFor({...run,original:{...run.original,status:'no_entrypoint'}}),null);
assert.equal(JSON.stringify(run.original),original);
""")

    def test_repair_default_descriptor_labels_mode_switches_and_original_is_untouched(self):
        self.repair_preview_javascript(r"""
const preview=makeRepair(),original=JSON.stringify(preview.run.original);await startPreview(preview);
assert.equal(created[0].options.descriptor,seed.repairs[preview.run.id].descriptor);
assert.equal(preview.host.dataset.previewMode,'repair');assert.equal(preview.node.dataset.previewMode,'repair');
assert.match(downloads[0].url,/originals\/c{64}\.json$/);
for(const value of [preview.status.textContent,preview.start.textContent,preview.zoom.textContent]){assert.match(value,/修复版/);assert.match(value,/非原始交付/);assert.match(value,/不用于评分/);}
setPreviewMode(preview,'original');await settle();
assert.equal(created.at(-1).options.descriptor,preview.run.original);assert.equal(preview.mode,'original');
assert.equal(preview.host.dataset.previewMode,'original');assert.equal(preview.status.textContent,'实时原作 · 非录像');
assert.equal(preview.start.textContent,'停止原作');assert.equal(preview.zoom.textContent,'放大原作');
const count=created.length;setPreviewMode(preview,'static');await settle();
assert.equal(preview.runtime,null);assert.equal(created.length,count);assert.equal(preview.host.style.visibility,'hidden');
assert.equal(preview.node.dataset.previewState,'static');assert.equal(preview.start.disabled,true);
assert.match(preview.status.textContent,/静态历史截图.*原始交付.*不是修复版/);
assert.equal(preview.controls.buttons[2]['aria-pressed'],'true');resumeCards();await settle();assert.equal(created.length,count);
setPreviewMode(preview,'repair');await settle();assert.equal(created.length,count+1);
assert.equal(preview.controls.buttons[0]['aria-pressed'],'true');assert.equal(preview.controls.buttons[1]['aria-pressed'],'false');
assert.equal(JSON.stringify(preview.run.original),original);
""")

    def test_repair_loading_switch_aborts_and_late_callbacks_cannot_revive_previous_mode(self):
        self.repair_preview_javascript(r"""
let resolveSDK;sdkReady=new Promise(resolve=>resolveSDK=resolve);
const preview=makeRepair(),pending=startPreview(preview);
assert.match(preview.status.textContent,/修复版.*非原始交付.*不用于评分.*装载中/);
setPreviewMode(preview,'static');assert.equal(downloads[0].signal.aborted,true);
resolveSDK(sdk);await pending;assert.equal(created.length,0);assert.equal(preview.runtime,null);
setPreviewMode(preview,'repair');await settle();const old=created.at(-1);
setPreviewMode(preview,'original');await settle();const current=preview.runtime,status=preview.status.textContent,token=preview.token;
old.options.onState({status:'loaded',message:'late repair'});old.options.onDiagnostic({kind:'error',message:'late repair error'});
assert.equal(preview.runtime,current);assert.equal(preview.status.textContent,status);assert.equal(preview.token,token);
setPreviewMode(preview,'repair');await settle();const latest=created.at(-1);togglePreview(preview);
assert.equal(preview.paused,true);assert.equal(preview.runtime,null);assert.match(preview.status.textContent,/修复版.*不用于评分.*已停止.*历史截图/);
latest.options.onState({status:'loaded'});latest.options.onDiagnostic({kind:'error',message:'late stopped error'});
assert.equal(preview.runtime,null);assert.equal(preview.node.dataset.previewState,'stopped');
resumeCards();await settle();assert.equal(preview.runtime,null);
""")

    def test_repair_transport_and_runtime_failures_use_historical_not_original_success_fallback(self):
        self.repair_preview_javascript(r"""
for(const kind of ['transport','state','diagnostic']){
 const preview=makeRepair(String(downloads.length+1).repeat(32));
 if(kind==='transport')global.fetch=async()=>{throw new Error('Package digest mismatch');};
 else global.fetch=async()=>response();
 await startPreview(preview);
 if(kind==='state')created.at(-1).options.onState({status:'error',message:'entry hash mismatch'});
 if(kind==='diagnostic')created.at(-1).options.onDiagnostic({kind:'error',message:'repair script failed'});
 assert.equal(preview.runtime,null);assert.equal(preview.paused,true);assert.equal(preview.node.dataset.previewState,'error');
 assert.equal(preview.host.style.visibility,'hidden');assert.equal(preview.host.dataset.previewMode,'repair');
 assert.match(preview.status.textContent,/修复版，非原始交付.*不用于评分.*静态历史截图回退（非修复结果）/);
 assert.doesNotMatch(preview.status.textContent,/原作启动失败|原作运行错误|实时原作/);
 const status=preview.status.textContent;resumeCards();await settle();assert.equal(preview.status.textContent,status);
}
""")

    def test_repair_can_switch_to_unsupported_original_and_original_error_policy_is_unchanged(self):
        self.repair_preview_javascript(r"""
const preview=makeRepair(undefined,'unsupported');await startPreview(preview);const count=created.length;
setPreviewMode(preview,'original');await settle();assert.equal(created.length,count);assert.equal(preview.runtime,null);
assert.equal(preview.start.disabled,true);assert.match(preview.status.textContent,/原作不可运行（unsupported）.*历史截图/);
assert.equal(preview.host.dataset.previewMode,'original');assert.equal(preview.zoom.textContent,'放大回退');
setPreviewMode(preview,'repair');await settle();assert.equal(created.length,count+1);
const other=makeRepair('2'.repeat(32));setPreviewMode(other,'original');await settle();
const instance=created.at(-1);instance.options.onDiagnostic({kind:'policy',message:'connect-src blocked'});
assert.equal(other.runtime,instance.runtime);assert.match(other.status.textContent,/隔离策略诊断.*非验收/);
instance.options.onDiagnostic({kind:'error',message:'Assignment to constant variable.'});
assert.equal(other.runtime,null);assert.equal(other.paused,true);assert.equal(other.node.dataset.previewState,'error');
assert.match(other.status.textContent,/原作运行错误（非验收）.*Assignment to constant variable/);
assert.doesNotMatch(other.status.textContent,/修复版/);
""")

    def test_repair_notice_and_mode_buttons_keep_hashes_scoring_and_original_downloads_explicit(self):
        self.repair_preview_javascript(r"""
const node=(tag,attrs={},children=[])=>({tag,attrs,children});
const preview=makeRepair(),notice=repairNotice(preview.run),modes=previewModes(preview.run,'repair');
const flatten=value=>[value.attrs?.text||'',...(value.children||[]).map(flatten)].join(' '),content=flatten(notice);
assert.match(content,/修复版，非原始交付.*不用于评分.*历史评分仍针对原始交付/);
assert.match(content,/完整查看器与离线文件保留原始字节，不含修复版/);
assert.ok(content.includes('源 SHA-256：'+'a'.repeat(64)));assert.ok(content.includes('结果 SHA-256：'+'b'.repeat(64)));
assert.ok(content.includes(seed.repairs[preview.run.id].review.reason));
assert.deepEqual(modes.children.map(button=>button.attrs['data-preview-mode']),['repair','original','static']);
assert.deepEqual(modes.children.map(button=>button.attrs['aria-pressed']),[true,false,false]);
assert.ok(modes.children.every(button=>button.attrs['data-preview-run']===preview.run.id));
""", [('repairNotice', False), ('previewModes', False)])

    def test_no_entrypoint_runs_gain_no_repair_controls_and_absent_mapping_keeps_original_buttons(self):
        self.repair_preview_javascript(r"""
const state={selected:new Set(),source:'scope'},a=/^media\/[a-f0-9]{64}\.(jpg|jpeg|png|webp|gif)$/;
const node=(tag,attrs={},children=[])=>({tag,attrs,children,style:{},append(...values){this.children.push(...values);}});
const c=()=> '未交付，没有可展示 SVG/HTML',scoreCard=()=>node('div'),meterText=()=>'',text=String,o=String,shownScores=()=>[],n=()=>'';
const flatten=element=>[element,...element.children.flatMap(flatten)];
for(let index=1;index<=8;index++){
 const preview=makeRepair(String(index).repeat(32));preview.run.original.status='no_entrypoint';delete preview.run.image;
 const elements=flatten(card(preview.run));
 assert.equal(elements.some(element=>element.attrs['data-preview-mode']),false);
 assert.equal(elements.some(element=>element.attrs.class==='work-repair'),false);
 const start=elements.find(element=>element.attrs['data-run-original']);assert.equal(start.attrs.text,'运行原作');assert.equal(start.attrs.disabled,true);
 const zoom=elements.find(element=>element.attrs['data-zoom-run']);assert.equal(zoom.attrs.disabled,true);
}
const preview=makeRepair('f'.repeat(32));delete seed.repairs;
const elements=flatten(card(preview.run));assert.equal(elements.some(element=>element.attrs['data-preview-mode']),false);
assert.equal(elements.find(element=>element.attrs['data-run-original']).attrs.text,'运行原作');
assert.equal(elements.find(element=>element.attrs['data-zoom-run']).attrs.text,'放大原作');
""", [('card', False), ('repairNotice', False), ('previewModes', False)])

    def test_repair_modal_mode_events_stop_close_and_late_sdk_do_not_resurrect_sandbox(self):
        self.repair_preview_javascript(r"""
const handlers=new Map();
class FakeNode {
 constructor(tag='div',attrs={},children=[]){this.tag=tag;this.attrs=attrs;this.children=children;this.dataset={};this.hidden=false;this.open=false;this.style={};this.events=new Map();this.isConnected=true;for(const [key,value]of Object.entries(attrs))this.setAttribute(key,value);}
 setAttribute(key,value){this.attrs[key]=value;if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase())]=String(value);if(key==='text')this.textContent=value;}
 replaceChildren(...children){this.children=children;}append(...children){this.children.push(...children);}
 addEventListener(type,callback){if(!this.events.has(type))this.events.set(type,[]);this.events.get(type).push(callback);}
 emit(type){for(const callback of this.events.get(type)||[])callback({});}
 showModal(){this.open=true;}close(){this.open=false;this.emit('close');}
 closest(){return this;}
 querySelectorAll(selector){return this.children.flatMap(child=>[...(selector==='[data-preview-mode]'&&child.dataset?.previewMode?[child]:[]),...(child.querySelectorAll?.(selector)||[])]);}
}
const node=(tag,attrs,children)=>new FakeNode(tag,attrs,children),a=/^media\/[a-f0-9]{64}\.(jpg|jpeg|png|webp|gif)$/;
for(const id of ['public-purpose','public-model','public-sort','public-score-source','public-mixed','public-history','public-clear','public-compare','original-dialog','original-title','original-content','public-zoom','public-zoom-image','public-theme'])dialogs['#'+id]=new FakeNode();
const controller=new AbortController(),scopes=[],n=()=>'',populateHistory=()=>{},narrow={addEventListener(){}},state={};let imageObserver;
global.document.addEventListener=(type,callback)=>handlers.set(type,callback);
global.window={addEventListener(){}};global.IntersectionObserver=class {observe(){}disconnect(){}};
const preview=makeRepair(),records=new Map([[preview.run.id,preview.run]]);
f();await startPreview(preview);assert.equal(preview.runtime!==null,true);
const click=attrs=>handlers.get('click')({target:new FakeNode('button',attrs)});
click({'data-zoom-run':preview.run.id});await settle();
const modal=activePreview;assert.equal(dialogs['#original-dialog'].open,true);assert.equal(modal.mode,'repair');assert.equal(preview.runtime,null);
assert.equal(modal.host.dataset.previewMode,'repair');assert.match(dialogs['#original-title'].textContent,/修复版.*非原始交付.*不用于评分/);
assert.equal(modal.fallback.hidden,true);assert.equal(modal.host.hidden,false);
const original=JSON.stringify(preview.run.original),first=created.at(-1);
click({'data-preview-mode':'static','data-preview-run':preview.run.id});await settle();
assert.equal(modal.runtime,null);assert.equal(modal.start.disabled,true);assert.equal(modal.fallback.hidden,false);assert.equal(modal.host.hidden,true);
assert.match(modal.status.textContent,/静态历史截图/);assert.match(dialogs['#original-title'].textContent,/历史截图/);
first.options.onDiagnostic({kind:'error',message:'late modal error'});assert.equal(modal.node.dataset.previewState,'static');
click({'data-preview-mode':'original','data-preview-run':preview.run.id});await settle();
assert.equal(modal.mode,'original');assert.equal(created.at(-1).options.descriptor,preview.run.original);
assert.doesNotMatch(dialogs['#original-title'].textContent,/修复版/);
click({'data-preview-mode':'repair','data-preview-run':preview.run.id});await settle();
modal.start.emit('click');assert.equal(modal.runtime,null);assert.equal(modal.paused,true);assert.match(modal.status.textContent,/修复版.*已停止/);
let resolveSDK;sdkReady=new Promise(resolve=>resolveSDK=resolve);modal.start.emit('click');await settle();
assert.equal(modal.loading,true);preview.paused=true;const count=created.length,signal=modal.abort.signal;
click({'data-public-close':'original-dialog'});assert.equal(signal.aborted,true);assert.equal(activePreview,null);assert.equal(modal.alive,false);
assert.equal(dialogs['#original-dialog'].open,false);assert.deepEqual(dialogs['#original-content'].children,[]);
resolveSDK(sdk);await settle();assert.equal(created.length,count);assert.equal(modal.runtime,null);assert.equal(preview.runtime,null);
assert.equal(JSON.stringify(preview.run.original),original);
""", [('g', False), ('cancelFull', False), ('repairNotice', False), ('previewModes', False), ('f', False)])

    def test_shared_numeric_decoder_initialization_bounds_and_exact_output_keys(self):
        program = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[1],'utf8'),end=code.indexOf('\n  const $=');
assert.ok(end>0);const prefix=code.slice(0,end)+'})();';
const scope=['visual-v1','label-unverified',true,'Synthetic reviewer'];
const decode=values=>{const seed={version:3,format:'progressive-static-v3',score_encoding:'scope-index-v1',score_scopes:[scope],runs:[{id:'scored',scores:[[0,7,values]]},{id:'unscored'}]};vm.runInNewContext(prefix,{window:{BENCH_GALLERY:seed}});return seed.runs;};
for(const values of [[0,4,null,1,2,3],[null,null,null,null,null,null]]){
 const rows=decode(values);assert.deepEqual(rows.map(row=>row.id),['scored','unscored']);
 assert.deepEqual(Object.keys(rows[0].scores[0]),['scope','index','values']);
 assert.equal(rows[0].scores[0].scope,JSON.stringify(scope));assert.equal(rows[0].scores[0].index,7);
 assert.deepEqual(Array.from(rows[0].scores[0].values),values);assert.deepEqual(Object.keys(rows[1]),['id']);
}
for(const invalid of [-0.01,4.01,NaN,Infinity,-Infinity,'2',false,{},[],undefined])assert.throws(()=>decode([invalid,0,1,2,3,4]),/gallery score encoding/);
"""
        result = subprocess.run(['node', '-e', program, str(ROOT / 'web/public-gallery.js')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_history_filter_equivalence_retains_ids_order_counts_pages_and_text(self):
        self.javascript([('matches', False), ('sortedRuns', False), ('cardRuns', False), ('render', False)], """
const text=value=>value==null||value===''?'unknown':String(value);
const state={task:'',purpose:'',model:'',from:'',to:'',sort:'',source:'scope',page:0};
const scopes=[],scoreLabels={compliance:'指令遵循'};
const seed={runs:[
 {id:'a',task_id:'media',purpose:'benchmark',model:'Alpha',registered_media:true,date:'2026-10-08',value:0},
 {id:'b',task_id:'media',purpose:'benchmark',model:'Beta',registered_media:false,date:null,value:4},
 {id:'c',task_id:'media',purpose:'smoke',model:'Alpha',registered_media:true,date:'2026-10-10',value:4},
 {id:'d',task_id:'history',purpose:'benchmark',model:'Alpha',registered_media:false,date:'2026-10-09',value:null},
 {id:'e',task_id:'history',purpose:'benchmark',model:'Beta',registered_media:false,date:'2026-10-09',value:0},
 {id:'f',task_id:'media',purpose:'benchmark',model:'Alpha',registered_media:true,date:'2026-10-08',value:null}
]};
const nodes=new Map(),$=key=>{if(!nodes.has(key))nodes.set(key,{hidden:false,open:false,value:'',textContent:'',options:[{value:state.purpose}],children:[],closest(){return this;},replaceChildren(...children){this.children=children;},append(...children){this.children.push(...children);}});return nodes.get(key);};
const document={querySelectorAll:()=>[]},imageObserver={disconnect(){}},clearCards=()=>{},pageSize=()=>2;
let historyRuns=[];const scoreValue=run=>run.value,card=run=>({id:run.id}),node=(tag,attrs)=>({tag,...attrs});
const populateModels=()=>{},populateHistory=()=>{},selection=()=>{},observeImages=()=>{},observePreviews=()=>{};
function oldSort(rows){
 if(!state.sort)return rows;
 const groups=new Map();for(const run of rows){const key=JSON.stringify([run.task_id,run.purpose,state.source]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(run);}
 return [...groups].sort(([a],[b])=>a<b?-1:a>b?1:0).flatMap(([,group])=>group.map((run,index)=>({run,index,value:run.value})).sort((a,b)=>(a.value===null)-(b.value===null)||(a.value!==null&&b.value!==null?b.value-a.value:0)||a.index-b.index).map(item=>item.run));
}
for(const task of ['', 'media','history','absent'])for(const sort of ['', 'compliance'])for(const model of ['', ' alpha ', 'absent'])for(const from of ['', '2026-10-09'])for(const page of [0,1,99]){
 Object.assign(state,{task,sort,model,from,page,purpose:'',to:''});
 const rows=seed.runs.filter(matches),registered=rows.filter(run=>run.registered_media),history=rows.filter(run=>!run.registered_media);
 const expected=oldSort(sort||model.trim()?rows:registered),pages=Math.max(1,Math.ceil(expected.length/2)),clamped=Math.min(page,pages-1);
 const visibleHistory=!sort&&!model.trim()&&Boolean(history.length);
 render();assert.equal(state.page,clamped);
 assert.deepEqual($('#public-grid').children.filter(item=>item.id).map(item=>item.id),expected.slice(clamped*2,(clamped+1)*2).map(run=>run.id));
 assert.deepEqual(historyRuns.map(run=>run.id),history.map(run=>run.id));
 assert.equal($('#public-history').hidden,!visibleHistory);assert.equal($('#public-history').open,visibleHistory&&!registered.length);
 assert.equal($('#public-result-label').textContent,`${rows.length} 最新 · 无媒体 ${history.length}`);
 assert.equal($('#public-score-coverage').textContent,`指令遵循 ${rows.filter(run=>run.value!==null).length}/${rows.length} 已评`);
 assert.equal($('#public-history-label').textContent,`${history.length} 次无公开媒体 · 评分、计量及全部失败记录`);
 assert.equal($('#public-models summary').textContent,model.trim()?'模型 / 来源 · 已筛选':'模型 / 来源');
}
""")

    def test_local_score_render_names_preserve_attribute_keys_values_and_text(self):
        self.javascript([('scoreCard', False)], """
const state={source:'scope',sort:'compliance'},scope=['visual-v1','label-unverified',true,'Synthetic reviewer'];
const scopeLookup=new Map([['scope',scope]]),scoreKeys=['compliance','recognizability','motion','visual_detail','interaction','completeness'];
const scoreLabels=Object.fromEntries(scoreKeys.map(key=>[key,key]));
const finite=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
const rankable=value=>value===scope,shortScope=()=> '盲评 · 来源标签未核实';
const node=(tag,attrs={},children=[])=>({tag,attrs,children});
const shownScores=run=>state.source==='*'?run.scores:[run.scores?.find(item=>item.scope===state.source)];
const summary={scope:'scope',index:7,values:[0,4,null,-1,5,'2']},run={scores:[summary]};
const result=scoreCard(run),sheet=result.children[0].children[0];
assert.deepEqual(result.attrs,{class:'work-scores'});assert.deepEqual(result.children[0].attrs,{class:'work-score','data-score-scope':'scope'});
assert.deepEqual(sheet.children.map(cell=>cell.attrs['data-dimension']),scoreKeys);
assert.equal(sheet.children[0].attrs.class,'score-cell active');
assert.deepEqual(sheet.children[0].children[1].children.map(item=>item.attrs.text),[0,'/4']);
assert.deepEqual(sheet.children[1].children[1].children.map(item=>item.attrs.text),[4,'/4']);
for(const cell of sheet.children.slice(2))assert.equal(cell.children[1].children[0].attrs.text,'分项缺失');
assert.equal(result.children[0].children[1].attrs.text,'盲评 · 来源标签未核实');
state.source='*';assert.equal(scoreCard(run).children[0].children[1].attrs.text,'口径 1 · 盲评 · 来源标签未核实');
state.source='absent';assert.equal(scoreCard(run).children[0].children[0].children[0].children[1].children[0].attrs.text,'本来源无记录');
""")

    def test_local_full_handoff_names_preserve_selected_mixed_and_route_output(self):
        self.preview_javascript("""
const state={selected:new Set(['a','b']),mixed:true,task:'crocodile',purpose:'benchmark',model:' Alpha ',source:'scope',sort:'compliance'};
const records=new Map([['a',{id:'a',task_id:'crocodile',purpose:'benchmark'}],['b',{id:'b',task_id:'crocodile',purpose:'smoke'}]]);
let fatal=false;const notice=()=>{},mountFull=async()=>{},actions=[];
global.window={async BENCH_GALLERY_ACTION(value){actions.push(value);}};
global.history={pushState(...args){location.href=args.at(-1).href;},replaceState(...args){location.href=args.at(-1).href;}};
await fullAction({kind:'compare',view:'gallery'});
assert.deepEqual(actions,[{kind:'compare',view:'gallery',selected:['a','b'],mixed:true,score_source:'scope',score_sort:'compliance'}]);
assert.equal(fullPending,false);assert.equal(new URL(location.href).searchParams.get('task_id'),'crocodile');
assert.equal(new URL(location.href).searchParams.get('purpose'),'');assert.equal(new URL(location.href).searchParams.has('q'),false);
await fullAction({kind:'detail',view:'evidence',run:'b'});
assert.deepEqual(actions.at(-1),{kind:'detail',view:'evidence',run:'b',selected:['a','b'],mixed:true,score_source:'scope',score_sort:'compliance'});
assert.equal(new URL(location.href).searchParams.get('purpose'),'smoke');assert.equal(new URL(location.href).searchParams.get('q'),'b');
""", [('fullAction', True)])

    def test_inline_default_viewport_matches_explicit_size_and_modal_fit_is_unchanged(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture();
function make(viewport){const host=new Element(),states=[],options={runId:'synthetic-run',descriptor,transport:'external',onState:value=>states.push({...value})};if(viewport!==undefined)options.viewport=viewport;return {host,states,runtime:BenchPreview.create(host,options)};}
const implicit=make(),explicit=make({width:1440,height:900}),modal=make('fit');
await Promise.all([implicit.runtime.start(bytes),explicit.runtime.start(bytes),modal.runtime.start(bytes)]);
assert.deepEqual(implicit.states,explicit.states);assert.deepEqual(implicit.runtime.getState(),explicit.runtime.getState());
assert.deepEqual(implicit.host.children[0].style,explicit.host.children[0].style);
assert.deepEqual(implicit.host.children[0].children[0].style,explicit.host.children[0].children[0].style);
assert.equal(implicit.runtime.getState().width,1440);assert.equal(implicit.runtime.getState().height,900);
assert.equal(modal.runtime.getState().width,800);assert.equal(modal.runtime.getState().height,450);
for(const unit of [implicit,explicit,modal])unit.runtime.destroy();
""")

    def test_runtime_supplied_bytes_are_copied_before_await_and_descriptor_is_owned(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture();
const buffer=new Uint8Array(bytes.length+2);buffer.set(bytes,1);const supplied=buffer.subarray(1,-1);
const {host,states,runtime}=instance(descriptor,value=>{if(value.status==='fetching')supplied.fill(0);});
const pending=runtime.start(supplied);
supplied.fill(0);descriptor.package.sha256='0'.repeat(64);descriptor.entry_sha256='0'.repeat(64);
await pending;
assert.equal(fetches,0);assert.equal(runtime.getState().status,'loading');
assert.deepEqual(states.slice(states.indexOf('fetching'),states.indexOf('fetching')+3),['fetching','verifying','loading']);assert.equal(host.children.length,1);
assert.equal(host.children[0].children[0].attributes.sandbox,'allow-scripts');
assert.match(host.children[0].children[0].srcdoc,/Content-Security-Policy/);
runtime.destroy();assert.equal(host.children.length,0);
assert.equal(observers.at(-1).disconnected,1);assert.equal(channels.at(-1).port1.closed,1);assert.equal(channels.at(-1).port2.closed,1);
for(const callbacks of listeners.values())assert.equal(callbacks.size,0);
""")

    def test_runtime_rejects_prepared_html_and_invalid_byte_inputs_without_fetch(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture(),{host,runtime}=instance(descriptor);
for(const value of [{html:'caller-prepared executable HTML'},Array.from(bytes),bytes.buffer,new Uint8Array(bytes.length+1),new Uint8Array(4*1024*1024+1)]){
 await assert.rejects(runtime.start(value),/Package size mismatch/);
 assert.equal(fetches,0);assert.equal(host.children.length,0);
}
const spoofed=new Uint8Array(4*1024*1024+1);Object.defineProperty(spoofed,'length',{value:bytes.length});
await assert.rejects(runtime.start(spoofed),/Package size mismatch/);assert.equal(host.children.length,0);
assert.equal(fetches,0);runtime.destroy();
""")

    def test_runtime_supplied_bytes_still_run_all_package_file_and_adaptation_checks(self):
        self.runtime_javascript("""
const cases=[
 [pack=>{pack.run_id='another-run';},/binding mismatch/],
 [pack=>{pack.files['index.html'].sha256='0'.repeat(64);},/File integrity mismatch/],
 [pack=>{pack.files['index.html'].base64+=' ';},/Invalid base64/],
 [pack=>{pack.adaptation.head_offset++;},/Head offset mismatch/],
 [pack=>{pack.unexpected=true;},/Invalid original package fields/]
];
for(const [change,message] of cases){
 const {bytes,descriptor}=await fixture(change),{host,runtime}=instance(descriptor);
 await runtime.start(bytes);assert.equal(runtime.getState().status,'error');assert.match(runtime.getState().message,message);
 assert.equal(host.children.length,0);runtime.destroy();
}
for(const kind of ['package-hash','entry-hash','duplicate-key','file-size']){
 const f=await fixture();
 if(kind==='package-hash')f.bytes[0]^=1;
 if(kind==='entry-hash')f.descriptor.entry_sha256='0'.repeat(64);
 if(kind==='duplicate-key'){
  f.bytes=new TextEncoder().encode(new TextDecoder().decode(f.bytes).replace('"version":1','"version":1,"version":1'));
  f.descriptor.package={size:f.bytes.length,sha256:await hash(f.bytes)};f.descriptor.package.path='originals/'+f.descriptor.package.sha256+'.json';
 }
 if(kind==='file-size'){
  f.pack.files['index.html'].size++;f.bytes=new TextEncoder().encode(JSON.stringify(f.pack));
  f.descriptor.package={size:f.bytes.length,sha256:await hash(f.bytes)};f.descriptor.package.path='originals/'+f.descriptor.package.sha256+'.json';
 }
 const {host,runtime}=instance(f.descriptor);await runtime.start(f.bytes);
 assert.equal(runtime.getState().status,'error');assert.equal(host.children.length,0);
 assert.match(runtime.getState().message,kind==='package-hash'?/Package digest mismatch/:kind==='entry-hash'?/Entry integrity mismatch/:kind==='duplicate-key'?/Duplicate JSON key/:/File integrity mismatch/);
 runtime.destroy();
}
assert.equal(fetches,0);
""")

    def test_runtime_supplied_svg_bytes_keep_validation_and_opaque_wrapper(self):
        self.runtime_javascript("""
const valid=await fixture(()=>{},true),{host,runtime}=instance(valid.descriptor);
await runtime.start(valid.bytes);assert.equal(runtime.getState().status,'loading');assert.equal(svgParses,1);
assert.match(host.children[0].children[0].srcdoc,/data:image\\/svg\\+xml;base64/);runtime.destroy();
for(const source of ['<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>','<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>','<svg xmlns="http://www.w3.org/2000/svg" href="https://example.test/x"/>']){
 const f=await fixture(()=>{},true),raw=new TextEncoder().encode(source),file=f.pack.files['original.svg'];
 Object.assign(file,{size:raw.length,sha256:await hash(raw),base64:Buffer.from(raw).toString('base64')});
 f.bytes=new TextEncoder().encode(JSON.stringify(f.pack));f.descriptor.entry_sha256=file.sha256;
 f.descriptor.package={size:f.bytes.length,sha256:await hash(f.bytes)};f.descriptor.package.path='originals/'+f.descriptor.package.sha256+'.json';
 const {host,runtime}=instance(f.descriptor);await runtime.start(f.bytes);assert.equal(runtime.getState().status,'error');assert.equal(host.children.length,0);runtime.destroy();
}
assert.equal(fetches,0);
""")

    def test_runtime_supplied_bytes_stale_prepare_stop_and_default_fetch_abort(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture(),{host,runtime}=instance(descriptor);
const first=runtime.start(bytes);runtime.stop();await first;
assert.equal(runtime.getState().status,'stopped');assert.equal(host.children.length,0);
const older=runtime.start(bytes),newer=runtime.start(bytes);await Promise.all([older,newer]);
assert.equal(host.children.length,1);assert.equal(runtime.getState().status,'loading');assert.equal(fetches,0);
runtime.stop();let signal;
global.fetch=async(url,options)=>{
 fetches++;signal=options.signal;assert.equal(url,'https://example.test/gallery/'+descriptor.package.path);
 assert.equal(options.cache,'no-store');assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');
 return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('cancelled','AbortError')),{once:true}));
};
const fetching=runtime.start();assert.equal(fetches,1);runtime.stop();await fetching;
assert.equal(signal.aborted,true);assert.equal(runtime.getState().status,'stopped');assert.equal(host.children.length,0);
await runtime.start(bytes);assert.equal(fetches,1);assert.equal(host.children.length,1);runtime.destroy();
""")

    def test_runtime_native_byte_promises_ignore_spoofed_then_and_copy_on_fulfillment(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture();let resolve,spoofCalls=0;
const promised=new Promise(done=>resolve=done);
promised.then=()=>{spoofCalls++;throw new Error('shadowed then must not run');};
const {host,runtime}=instance(descriptor,value=>{if(value.status==='verifying')bytes.fill(0);});
const pending=runtime.start(promised);resolve(bytes);
// This runs after the native fulfillment handler, but before its await continuation.
queueMicrotask(()=>bytes.fill(0));await pending;
assert.equal(spoofCalls,0);assert.equal(fetches,0);assert.equal(host.children.length,1);
assert.equal(runtime.getState().status,'loading');runtime.destroy();
""")

    def test_runtime_byte_promises_reject_invalid_fulfillment_and_non_native_inputs(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture();let thenCalls=0;
for(const value of [undefined,null,{html:'prepared HTML'},Array.from(bytes),bytes.buffer,new Uint8Array(bytes.length+1)]){
 const {host,runtime}=instance(descriptor);await runtime.start(Promise.resolve(value));
 assert.equal(runtime.getState().status,'error');assert.match(runtime.getState().message,/Package size mismatch/);
 assert.equal(host.children.length,0);runtime.destroy();
}
const {host,runtime}=instance(descriptor);
for(const value of [{then(){thenCalls++;}},Object.assign(Object.create(Promise.prototype),{then(){thenCalls++;}}),new (class extends Promise {})(resolve=>resolve(bytes))]){
 await assert.rejects(runtime.start(value));assert.equal(host.children.length,0);
}
assert.equal(thenCalls,0);assert.equal(fetches,0);
await runtime.start(Promise.reject(new Error('controlled reader rejection')));
assert.equal(runtime.getState().status,'error');assert.match(runtime.getState().message,/controlled reader rejection/);
runtime.destroy();
""")

    def test_runtime_byte_promise_destroy_restart_and_reentrant_state_are_stale_safe(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture(),unhandled=[];
process.on('unhandledRejection',error=>unhandled.push(error));
const settle=()=>new Promise(resolve=>setImmediate(resolve));
for(const phase of ['before-resolution','after-resolution']){
 let resolve,copies=0;const promised=new Promise(done=>resolve=done);
 const stale=new Proxy(bytes,{getPrototypeOf(target){copies++;return Reflect.getPrototypeOf(target);}});
 const {host,runtime,states}=instance(descriptor),pending=runtime.start(promised);
 if(phase==='after-resolution')resolve(stale);
 runtime.destroy();const count=states.length;if(phase==='before-resolution')resolve(stale);
 await pending;assert.equal(copies,0);assert.equal(host.children.length,0);assert.equal(states.length,count);
 await assert.rejects(runtime.start(Promise.reject(new Error('already destroyed'))),/Preview destroyed/);
}
let resolve;const oldPromise=new Promise(done=>resolve=done),{runtime,host}=instance(descriptor);
const old=runtime.start(oldPromise);const latest=runtime.start(Promise.resolve(bytes));
resolve(undefined);await Promise.all([old,latest]);assert.equal(host.children.length,1);assert.equal(runtime.getState().status,'loading');runtime.destroy();
for(const stage of ['fetching','verifying','loading']){
 let current;const unit=instance(descriptor,value=>{if(value.status===stage)current.stop();});current=unit.runtime;
 await current.start(Promise.resolve(bytes));assert.equal(current.getState().status,'stopped');assert.equal(unit.host.children.length,0);current.destroy();
}
await settle();assert.deepEqual(unhandled,[]);assert.equal(fetches,0);
""")

    def test_package_download_overlaps_sdk_and_only_eligible_intents_fetch(self):
        self.preview_javascript("""
let resolveSDK;sdkReady=new Promise(resolve=>{resolveSDK=resolve;});
let resolvePackage;
global.fetch=(url,{signal})=>new Promise(resolve=>{downloads.push({url,signal});resolvePackage=resolve;});
const preview=makePreview('concurrent'),paused=makePreview('paused',true),offscreen=makePreview('offscreen');offscreen.visible=false;
await startPreview(paused);await startPreview(offscreen);
assert.equal(downloads.length,0);
const pending=startPreview(preview),token=preview.token;
assert.equal(downloads.length,1,'package fetch begins while the SDK is still pending');
assert.equal(downloads[0].url,'https://example.test/report/'+preview.run.original.package.path);
resumeCards();resumeCards();assert.equal(downloads.length,1);assert.equal(preview.token,token);
resolvePackage(response());await settle();assert.equal(created.length,0);assert.equal(reads.length,0,'body reader waits for SRI SDK');
resolveSDK(sdk);await pending;
assert.equal(created.length,1);assert.deepEqual(started,['concurrent']);assert.equal(downloads.length,1);
assert.equal(preview.node.dataset.previewState,'loaded');stopPreview(preview);
assert.equal(downloads[0].signal.aborted,true);
""")

    def test_sdk_failure_aborts_package_and_handles_both_rejections(self):
        self.preview_javascript("""
const unhandled=[];process.on('unhandledRejection',error=>unhandled.push(error));
let rejectSDK;sdkReady=new Promise((resolve,reject)=>{rejectSDK=reject;});
let aborts=0;
global.fetch=(url,{signal})=>new Promise((resolve,reject)=>{downloads.push({signal});signal.addEventListener('abort',()=>{aborts++;reject(new DOMException('package cancelled','AbortError'));},{once:true});});
const preview=makePreview('sdk-failure'),pending=startPreview(preview);
rejectSDK(new Error('SRI rejected'));await pending;await settle();
assert.equal(aborts,1);assert.equal(downloads[0].signal.aborted,true);
assert.equal(preview.runtime,null);assert.equal(preview.loading,false);assert.equal(preview.paused,true);
assert.equal(preview.node.dataset.previewState,'error');assert.match(preview.status.textContent,/SRI rejected/);
assert.equal(preview.start.textContent,'运行原作');assert.deepEqual(unhandled,[]);
// Package rejection is handled immediately even while the SDK remains pending.
let rejectLateSDK;sdkReady=new Promise((resolve,reject)=>{rejectLateSDK=reject;});
global.fetch=async()=>{throw new Error('package SHA256');};
preview.paused=false;await startPreview(preview);
assert.match(preview.status.textContent,/package SHA256/);
rejectLateSDK(new Error('later SDK failure'));await settle();assert.deepEqual(unhandled,[]);
""")

    def test_stop_and_restart_cancel_download_without_stale_promise_cleanup(self):
        self.preview_javascript("""
const unhandled=[];process.on('unhandledRejection',error=>unhandled.push(error));
let rejectSDK;sdkReady=new Promise((resolve,reject)=>{rejectSDK=reject;});
global.fetch=(url,{signal})=>new Promise((resolve,reject)=>{downloads.push({signal,resolve,reject});signal.addEventListener('abort',()=>reject(new DOMException('cancelled package','AbortError')),{once:true});});
const preview=makePreview('restart-download'),first=startPreview(preview);
togglePreview(preview);await first;
assert.equal(downloads[0].signal.aborted,true);assert.equal(preview.paused,true);
let resolveSDK;sdkReady=new Promise(resolve=>{resolveSDK=resolve;});
togglePreview(preview);const currentToken=preview.token;
assert.equal(downloads.length,2);assert.notEqual(downloads[0].signal,downloads[1].signal);
rejectSDK(new Error('old SDK rejected'));await settle();
assert.equal(preview.token,currentToken);assert.equal(preview.loading,true);assert.equal(created.length,0);
resolveSDK(sdk);downloads[1].resolve(response());await settle();
assert.equal(created.length,1);assert.equal(preview.node.dataset.previewState,'loaded');assert.deepEqual(unhandled,[]);
// A noncooperative old download completing after abort cannot replace a new runtime.
global.fetch=(url,{signal})=>new Promise((resolve,reject)=>downloads.push({signal,resolve,reject}));
stopPreview(preview);preview.paused=false;const older=startPreview(preview);
stopPreview(preview);const newer=startPreview(preview);
downloads[3].resolve(response());await newer;
const current=preview.runtime,status=preview.status.textContent,token=preview.token;
downloads[2].reject(new Error('late package rejection'));await older;await settle();
assert.equal(preview.runtime,current);assert.equal(preview.status.textContent,status);assert.equal(preview.token,token);
assert.equal(preview.loading,false);assert.deepEqual(unhandled,[]);
""")

    def test_offscreen_filter_and_hidden_cancel_inflight_package(self):
        self.preview_javascript("""
global.fetch=(url,{signal})=>new Promise((resolve,reject)=>{downloads.push({signal});signal.addEventListener('abort',()=>reject(new DOMException('cancelled','AbortError')),{once:true});});
for(const action of [c=>{c.visible=false;stopPreview(c);},()=>clearCards(),()=>{document.hidden=true;suspendCards();}]){
 const preview=makePreview('cancel-'+downloads.length),pending=startPreview(preview);
 action(preview);await pending;
 assert.equal(downloads.at(-1).signal.aborted,true);assert.equal(preview.runtime,null);
 assert.equal(preview.paused,false);assert.equal(preview.loading,false);assert.equal(created.length,0);
 document.hidden=false;cardPreviews.clear();
}
""")

    def test_stop_during_sdk_load_cancels_start_and_retains_manual_pause(self):
        self.preview_javascript("""
let resolveSDK;sdkReady=new Promise(resolve=>{resolveSDK=resolve;});
const preview=makePreview('loading');
const pending=startPreview(preview);
assert.equal(preview.node.dataset.previewState,'loading');
assert.equal(preview.start.textContent,'停止原作');
togglePreview(preview);
assert.equal(preview.paused,true);
assert.equal(preview.node.dataset.previewState,'stopped');
assert.equal(preview.start.textContent,'运行原作');
resolveSDK(sdk);await pending;
assert.equal(created.length,0);assert.equal(preview.runtime,null);
resumeCards();await settle();assert.equal(created.length,0);
togglePreview(preview);await settle();
assert.equal(preview.paused,false);assert.equal(created.length,1);
assert.equal(preview.node.dataset.previewState,'loaded');
togglePreview(preview);
assert.equal(preview.paused,true);assert.equal(preview.runtime,null);
assert.deepEqual(destroyed,['loading']);
""")

    def test_loading_resume_is_single_flight_and_restart_ignores_stale_tokens(self):
        self.preview_javascript("""
let resolveSDK;sdkReady=new Promise(resolve=>{resolveSDK=resolve;});
const preview=makePreview('restart');
const pending=startPreview(preview),token=preview.token;
resumeCards();resumeCards();
assert.equal(preview.token,token,'resume must not replace an in-flight load');
togglePreview(preview);
let resolveRestartSDK;sdkReady=new Promise(resolve=>{resolveRestartSDK=resolve;});
togglePreview(preview);
assert.equal(preview.paused,false);
assert.equal(preview.node.dataset.previewState,'loading');
assert.ok(preview.token>token);
resolveSDK(sdk);await pending;
assert.equal(created.length,0);assert.equal(preview.loading,true,'stale cleanup must retain the newer loading intent');
resolveRestartSDK(sdk);await settle();
assert.equal(created.length,1);assert.deepEqual(started,['restart']);
assert.equal(preview.node.dataset.previewState,'loaded');
""")

    def test_offscreen_and_visibility_suspension_cancel_loading_without_manual_pause(self):
        self.preview_javascript("""
let resolveSDK;sdkReady=new Promise(resolve=>{resolveSDK=resolve;});
const preview=makePreview('visible'),paused=makePreview('paused',true);
const pending=startPreview(preview);
preview.visible=false;stopPreview(preview);
resolveSDK(sdk);await pending;resumeCards();await settle();
assert.equal(created.length,0);assert.equal(preview.paused,false);
preview.visible=true;resumeCards();await settle();
assert.equal(created.length,1);assert.equal(paused.runtime,null);
document.hidden=true;suspendCards();resumeCards();await settle();
assert.equal(preview.runtime,null);assert.equal(created.length,1);
document.hidden=false;resumeCards();await settle();
assert.equal(created.length,2);assert.equal(paused.paused,true);
assert.equal(paused.runtime,null);
""")

    def test_failed_full_fetch_restores_registered_previews_without_unpausing_cards(self):
        self.preview_javascript("""
const state={selected:new Set(),mixed:false,task:'crocodile',purpose:'benchmark',model:'',source:'scope',sort:''};
const records=new Map();let fullPromise=null,fatal=false;
const notices=[];const notice=(message,error)=>notices.push({message,error});
const node=(tag,attributes)=>({tag,...attributes,addEventListener(type,callback){this[type]=callback;}});
dialogs['#public-notice']={hidden:true,classList:{add(){}},children:[],replaceChildren(...children){this.children=children;},append(child){this.children.push(child);}};
document.createTextNode=text=>({text});
global.location={href:'https://example.test/report/#gallery',origin:'https://example.test'};
global.history={pushState(){},replaceState(){}};
const fetches=[];
const readFull=signal=>new Promise((resolve,reject)=>fetches.push({signal,resolve,reject}));
const preview=makePreview('running'),paused=makePreview('paused',true);
await startPreview(preview);
const pending=fullAction({kind:'view',view:'matrix'});
assert.equal(fetches.length,1);assert.equal(preview.runtime,null);
assert.equal(cardPreviews.get('running'),preview);assert.equal(preview.alive,true);
resumeCards();await settle();assert.equal(created.length,1,'no restart during handoff');
fetches[0].reject(new Error('fetch unavailable'));await pending;await settle();
assert.equal(fullPromise,null);assert.equal(fullPending,false);
assert.equal(cardPreviews.size,2);assert.equal(cardPreviews.get('paused'),paused);
assert.equal(preview.node.dataset.previewState,'loaded');assert.equal(created.length,2);
assert.equal(paused.paused,true);assert.equal(paused.runtime,null);
const retry=dialogs['#public-notice'].children.at(-1);
assert.equal(retry.text,'重试此动作');
retry.click();assert.equal(fetches.length,2);assert.equal(preview.runtime,null);
fetches[1].reject(new Error('still unavailable'));await settle();
assert.equal(preview.node.dataset.previewState,'loaded');assert.equal(created.length,3);
assert.equal(paused.paused,true);assert.equal(paused.runtime,null);
await fullAction({kind:'compare',view:'gallery'});
assert.equal(fetches.length,2);assert.equal(preview.node.dataset.previewState,'loaded');
assert.equal(fullPending,false,'invalid comparison must not suspend the gallery');
const older=fullAction({kind:'view',view:'matrix'});
const newer=fullAction({kind:'view',view:'evidence'});
assert.equal(fetches.length,3,'handoffs share one fetch');
fetches[2].reject(new Error('shared fetch failure'));await Promise.all([older,newer]);await settle();
assert.equal(fullPending,false);assert.equal(created.length,4,'only the current intent resumes');
assert.equal(paused.paused,true);assert.equal(paused.runtime,null);
const cancelled=fullAction({kind:'view',view:'matrix'});
const cancelledFetch=fetches[3];cancelFull();
assert.equal(cancelledFetch.signal.aborted,true);assert.equal(fullPending,false);
resumeCards();await settle();assert.equal(created.length,5);
cancelledFetch.reject(new DOMException('cancelled','AbortError'));await cancelled;await settle();
assert.equal(created.length,5,'stale cancellation must not resume twice');
assert.equal(preview.node.dataset.previewState,'loaded');assert.equal(paused.runtime,null);
""", [('mountFull', True), ('fullAction', True), ('cancelFull', False)])

    def test_loaded_original_error_cleans_sandbox_falls_back_and_allows_manual_retry(self):
        self.preview_javascript("""
for(const inline of [true,false]){
 const preview=makePreview(inline?'inline-error':'modal-error');preview.inline=inline;
 if(!inline){activePreview=preview;dialogs['#original-dialog'].open=true;}
 Object.defineProperty(preview.status,'innerHTML',{set(){throw new Error('diagnostics must remain plain text');}});
 await startPreview(preview);
 const instance=created.at(-1),clears=preview.host.clears;
 preview.host.children=['sandbox'];
 assert.equal(preview.node.dataset.previewState,'loaded');
 assert.equal(typeof instance.options.onDiagnostic,'function');
 const message='Assignment to constant variable. <img src=x onerror=alert(1)>';
 instance.options.onDiagnostic({kind:'error',message});
 assert.equal(preview.runtime,null);assert.equal(preview.loading,false);
 assert.equal(preview.paused,true);assert.equal(preview.node.dataset.previewState,'error');
 assert.deepEqual(preview.host.children,[]);assert.equal(preview.host.clears,clears+1);
 assert.equal(destroyed.at(-1),preview.run.id);
 assert.equal(preview.start.disabled,false);assert.equal(preview.start.textContent,'运行原作');
 assert.ok(preview.status.textContent.includes(message));assert.match(preview.status.textContent,/原作运行错误.*非验收/);
 if(inline){assert.match(preview.status.textContent,/静态回退/);assert.equal(preview.host.style.visibility,'hidden');}
 const count=created.length;resumeCards();await settle();assert.equal(created.length,count);
 togglePreview(preview);await settle();
 assert.equal(preview.paused,false);assert.equal(created.length,count+1);
 assert.equal(preview.node.dataset.previewState,'loaded');assert.equal(preview.start.textContent,'停止原作');
 stopPreview(preview);preview.alive=false;cardPreviews.delete(preview.run.id);
 activePreview=null;dialogs['#original-dialog'].open=false;
}
""")

    def test_original_error_while_loading_blocks_late_loaded_state_and_cleans_resources(self):
        self.preview_javascript("""
let finishStart;
sdk.create=(host,options)=>{
 const runtime={start(){options.onState({status:'loading',message:'loading'});host.children=['sandbox'];return new Promise(resolve=>{finishStart=()=>{options.onState({status:'loaded',message:'late load'});resolve();};});},destroy(){destroyed.push(options.runId);options.onState({status:'stopped',message:'destroy callback'});}};
 created.push({host,options,runtime});return runtime;
};
const preview=makePreview('loading-error');
const pending=startPreview(preview);await settle();
assert.equal(preview.node.dataset.previewState,'loading');
created[0].options.onDiagnostic({kind:'error',message:'script initialization failed'});
const errorText=preview.status.textContent;
assert.equal(preview.node.dataset.previewState,'error');assert.equal(preview.runtime,null);
assert.equal(preview.paused,true);assert.equal(preview.host.style.visibility,'hidden');
assert.deepEqual(preview.host.children,[]);assert.deepEqual(destroyed,['loading-error']);
assert.equal(preview.start.textContent,'运行原作');assert.equal(preview.start.disabled,false);
finishStart();await pending;
assert.equal(preview.status.textContent,errorText);assert.equal(preview.node.dataset.previewState,'error');
assert.equal(preview.runtime,null);assert.equal(preview.loading,false);
""")

    def test_stale_original_diagnostics_cannot_overwrite_or_destroy_new_instance(self):
        self.preview_javascript("""
const preview=makePreview('stale-diagnostic');await startPreview(preview);
const old=created[0];old.options.onDiagnostic({kind:'error',message:'first instance error'});
togglePreview(preview);await settle();
const current=created[1],status=preview.status.textContent,token=preview.token;
old.options.onDiagnostic({kind:'error',message:'stale error'});
old.options.onDiagnostic({kind:'policy',message:'stale policy'});
old.options.onState({status:'error',message:'stale state'});
assert.equal(preview.runtime,current.runtime);assert.equal(preview.status.textContent,status);
assert.equal(preview.token,token);assert.equal(preview.node.dataset.previewState,'loaded');
assert.equal(preview.paused,false);assert.deepEqual(destroyed,['stale-diagnostic']);
for(const [target,key,value] of [[preview,'alive',false],[preview.node,'isConnected',false],[document,'hidden',true],[preview,'visible',false]]){
 const saved=target[key];target[key]=value;
 current.options.onDiagnostic({kind:'error',message:'inactive error'});
 current.options.onDiagnostic({kind:'policy',message:'inactive policy'});
 assert.equal(preview.runtime,current.runtime);assert.equal(preview.status.textContent,status);
 target[key]=saved;
}
assert.deepEqual(destroyed,['stale-diagnostic']);
""")

    def test_policy_diagnostics_are_bounded_text_and_survive_loaded_state_without_stopping(self):
        self.preview_javascript("""
const preview=makePreview('policy');await startPreview(preview);
const current=created[0];
current.options.onState({status:'loading',message:'loading'});
current.options.onDiagnostic({kind:'policy',message:'Blocked by isolation policy: connect-src <script>unsafe</script>'});
assert.match(preview.status.textContent,/隔离策略诊断.*非验收.*connect-src/);
current.options.onState({status:'loaded',message:'loaded'});
assert.match(preview.status.textContent,/实时原作.*非录像.*隔离策略诊断.*connect-src/);
current.options.onDiagnostic({kind:'policy',message:'x'.repeat(1000)});
assert.ok(preview.status.textContent.includes('x'.repeat(500)));
assert.ok(!preview.status.textContent.includes('x'.repeat(501)));
assert.equal(preview.status.textContent.split('隔离策略诊断').length-1,1);
assert.equal(preview.runtime,current.runtime);assert.equal(preview.node.dataset.previewState,'loaded');
assert.equal(preview.paused,false);assert.equal(preview.host.style.visibility,'visible');
assert.equal(preview.start.textContent,'停止原作');assert.deepEqual(destroyed,[]);
""")

    def test_viewer_paths_cannot_escape_the_same_directory(self):
        self.javascript([('pathURL', False)], """
global.location={href:'https://example.test/report/?purpose=benchmark#gallery',origin:'https://example.test'};
const name='viewer/'+'a'.repeat(64)+'.html';
assert.equal(pathURL(name,'html'),'https://example.test/report/'+name);
for(const path of ['../'+name,'/'+name,'https://evil.test/'+name,name+'?extra=1',name.replace('.html','.svg')]) assert.throws(()=>pathURL(path,'html'));
""")

    def test_legacy_queries_request_the_full_viewer_without_losing_empty_values(self):
        self.javascript([('readURL', False)], """
const seed={defaults:{task_id:'crocodile',purpose:'benchmark',score_source:'default-scope'}};
const scoreKeys=['compliance','motion'];
const scopeLookup=new Map([['default-scope',[]],['other-scope',[]]]);
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
for(const [query,sort,source] of [
 ['?score_sort=motion&score_source=other-scope','motion','other-scope'],
 ['?score_sort=motion&score_source=*','','*'],
 ['?score_sort=unknown&score_source=unknown','','default-scope']]) {
 global.location={search:query,hash:'#gallery'};
 assert.equal(readURL(),false);assert.equal(state.sort,sort);assert.equal(state.source,source);
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

    def test_score_sort_remains_null_last_descending_and_stable(self):
        self.javascript([('sortedRuns', False)], """
const state={sort:'compliance',source:'scope'};
const scoreValue=run=>run.value;
for(const values of [[null,0,-1,2,0,null,2], [null,null], [0,0], [-2,-1,0,1,2]]){
  const runs=values.map((value,index)=>({id:String(index),task_id:'crocodile',purpose:'benchmark',value}));
  const expected=runs.map((run,index)=>({run,index,value:run.value})).sort((a,b)=>(a.value===null)-(b.value===null)||(a.value!==null&&b.value!==null?b.value-a.value:0)||a.index-b.index).map(item=>item.run);
  assert.deepEqual(sortedRuns(runs),expected);
}
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

    def test_sdk_package_reader_native_manifest_binding_mime_redirect_and_cleanup(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture(),server=await packageServer(bytes);
try{
 const url=new URL(descriptor.package.path,document.baseURI).href;
 for(const fake of [{ok:true},Object.defineProperty(Object.create(Response.prototype),'url',{value:url})])await assert.rejects(BenchPreview.readPackage(descriptor,fake));
 // A genuine Response with a shadowed expected URL must not bypass native URL binding.
 const wrong=await nativeFetch(new URL('wrong.json',document.baseURI));Object.defineProperty(wrong,'url',{value:url});
 await assert.rejects(BenchPreview.readPackage(descriptor,wrong),/URL mismatch/);assert.equal(wrong.body.locked,false);
 for(const mode of ['mime','http','redirect','descriptor','inline']){
  server.mime=mode==='mime'?'text/html':'application/json';server.status=mode==='http'?403:200;
  let response;
  if(mode==='redirect'){
   server.redirect=true;server.target=url;response=await nativeFetch(url,{redirect:'manual'});server.redirect=false;
   // Native redirected Response comes from a followed one-hop redirect.
   const redirectServer=require('node:http').createServer((req,res)=>{res.writeHead(302,{Location:url});res.end();});
   await new Promise(resolve=>redirectServer.listen(0,'127.0.0.1',resolve));
   try{response=await nativeFetch('http://127.0.0.1:'+redirectServer.address().port+'/redirect');}
   finally{redirectServer.closeAllConnections();await new Promise(resolve=>redirectServer.close(resolve));}
  }else response=await nativeFetch(url);
  const changed=mode==='descriptor'?{...descriptor,package:{...descriptor.package,path:'originals/'+'0'.repeat(64)+'.json'}}:mode==='inline'?{...descriptor,package:{size:bytes.length,sha256:descriptor.package.sha256,base64:Buffer.from(bytes).toString('base64')}}:descriptor;
  await assert.rejects(BenchPreview.readPackage(changed,response));assert.equal(response.body.locked,false);
 }
 server.mime='application/json';server.status=200;
 const response=await nativeFetch(url),cleanup=trackReader(response);
 const pending=BenchPreview.readPackage(descriptor,response);descriptor.package.sha256='0'.repeat(64);descriptor.package.size=1;
 assert.deepEqual(await pending,bytes);assert.deepEqual(cleanup,{cancelled:1,released:1});assert.equal(fetches,0);
}finally{await server.closeNow();}
""")

    def test_sdk_package_reader_decoded_bounds_gzip_hash_and_single_fetch(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture(),server=await packageServer(bytes);
try{
 const url=new URL(descriptor.package.path,document.baseURI).href;
 for(const gzip of [false,true]){
  server.gzip=gzip;const response=await nativeFetch(url),cleanup=trackReader(response),before=server.requests;
  const {runtime,host}=instance(descriptor);await runtime.start(BenchPreview.readPackage(descriptor,response));
  assert.equal(server.requests,before);assert.equal(host.children.length,1);assert.equal(runtime.getState().status,'loading');
  assert.deepEqual(cleanup,{cancelled:1,released:1});runtime.destroy();
 }
 for(const mode of ['length','bad-length','overflow','truncated','hash']){
  server.gzip=mode!=='length';server.headerLength=mode==='length'?'1':mode==='bad-length'?'x':null;
  server.bytes=mode==='overflow'?new Uint8Array(bytes.length+1):mode==='truncated'?bytes.subarray(1):mode==='hash'?new Uint8Array(bytes.length):bytes;
  if(mode==='bad-length'){await assert.rejects(nativeFetch(url),/fetch failed/);continue;}
  const response=await nativeFetch(url),cleanup=trackReader(response);
  await assert.rejects(BenchPreview.readPackage(descriptor,response),mode==='length'||mode==='bad-length'?/response size mismatch/:mode==='overflow'?/too large/:mode==='truncated'?/truncated/:/digest mismatch/);
  assert.deepEqual(cleanup,{cancelled:mode==='length'||mode==='bad-length'?0:1,released:mode==='length'||mode==='bad-length'?0:1});assert.equal(response.body.locked,false);
 }
 assert.equal(fetches,0);
}finally{await server.closeNow();}
""")

    def test_sdk_package_reader_uses_native_headers_despite_shadowed_get(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture(),server=await packageServer(bytes);
try{
 const url=new URL(descriptor.package.path,document.baseURI).href;
 for(const mode of ['false-mime','false-length','false-encoding','valid-mime','valid-length','valid-gzip']){
  server.mime=mode==='false-mime'?'text/html':'application/json';
  server.gzip=mode==='valid-gzip';server.headerLength=mode==='false-length'||mode==='false-encoding'?'1':null;
  const response=await nativeFetch(url),cleanup=trackReader(response);let calls=0;
  const get=Headers.prototype.get.bind(response.headers);
  response.headers.get=name=>{
   calls++;
   if(name==='Content-Type')return mode==='false-mime'?'application/json':mode==='valid-mime'?'text/html':get(name);
   if(name==='Content-Length')return mode==='false-length'?String(bytes.length):mode==='valid-length'?'1':get(name);
   if(name==='Content-Encoding')return mode==='false-encoding'?'gzip':mode==='valid-gzip'?'identity':get(name);
   return get(name);
  };
  if(mode.startsWith('false-')){
   await assert.rejects(BenchPreview.readPackage(descriptor,response),mode==='false-mime'?/Package response mismatch/:/Package response size mismatch/);
   assert.deepEqual(cleanup,{cancelled:0,released:0});
  }else{
   assert.deepEqual(await BenchPreview.readPackage(descriptor,response),bytes);
   assert.deepEqual(cleanup,{cancelled:1,released:1});
  }
  assert.equal(calls,0,'caller-shadowed Headers.get must never execute');assert.equal(response.body.locked,false);
 }
 assert.equal(fetches,0);
}finally{await server.closeNow();}
""")

    def test_sdk_package_reader_abort_before_and_during_read_is_stale_safe(self):
        self.runtime_javascript("""
const {bytes,descriptor}=await fixture(),server=await packageServer(bytes);server.hold=true;
try{
 const url=new URL(descriptor.package.path,document.baseURI).href;
 for(const phase of ['before-reader','during-reader']){
  const controller=new AbortController(),response=await nativeFetch(url,{signal:controller.signal}),cleanup=trackReader(response);
  if(phase==='before-reader')controller.abort();
  const {host,runtime,states}=instance(descriptor),pending=runtime.start(BenchPreview.readPackage(descriptor,response));
  await new Promise(resolve=>setImmediate(resolve));runtime.destroy();const count=states.length;
  if(phase==='during-reader')controller.abort();
  await pending;assert.equal(host.children.length,0);assert.equal(states.length,count);assert.deepEqual(cleanup,{cancelled:1,released:1});
 }
 const controller=new AbortController();controller.abort();await assert.rejects(nativeFetch(url,{signal:controller.signal}),/abort/i);
 assert.equal(fetches,0);
}finally{await server.closeNow();}
""")

    def test_stop_after_headers_before_sdk_aborts_native_response_body_promptly(self):
        self.preview_javascript("""
const realFetch=nativeFetch;
const http=require('node:http'),server=http.createServer((req,res)=>{res.writeHead(200,{'Content-Type':'application/json','Content-Length':'3'});res.write(Buffer.from([7]));});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try{
 location.href='http://127.0.0.1:'+server.address().port+'/';location.origin=new URL(location.href).origin;
 let headersReady;const headers=new Promise(resolve=>headersReady=resolve);
 global.fetch=async(url,options)=>{const response=await realFetch(url,options);headersReady(response);return response;};
 let resolveSDK;sdkReady=new Promise(resolve=>resolveSDK=resolve);
 const preview=makePreview('headers-cancel'),pending=startPreview(preview),response=await headers;
 assert.equal(reads.length,0);assert.equal(created.length,0);stopPreview(preview);
 const reader=response.body.getReader();await assert.rejects(reader.closed,/abort/i);reader.releaseLock();
 resolveSDK(sdk);await pending;assert.equal(reads.length,0);assert.equal(created.length,0);
}finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
""")

    def test_decoded_stream_bounds_hash_mime_and_gzip_lengths(self):
        self.javascript([('readFull', True)], """
const crypto=require('node:crypto').webcrypto;
const digest=async bytes=>Buffer.from(await crypto.subtle.digest('SHA-256',bytes)).toString('hex');
const prefix='window.BENCH_SNAPSHOT=';
const data={format:'static-media-v2',transport:'external',runs:[{id:'r1'}]};
const program='trusted-test-viewer';
const payload=new TextEncoder().encode('synthetic complete document · 校验');
const seed={counts:{runs:1,full_runs:1},full:{path:'viewer/'+'a'.repeat(64)+'.html',size:payload.length,sha256:await digest(payload),script_sha256:await digest(new TextEncoder().encode(program))}};
const records=new Map([['r1',{}]]);
const pathURL=()=> 'https://example.test/report/viewer/a.html';
let parsed=0;
class DOMParser {parseFromString(text){parsed++;assert.equal(text,new TextDecoder().decode(payload));const scripts=[{textContent:prefix+JSON.stringify(data)+';',hasAttribute:()=>false,remove(){}},{textContent:program,hasAttribute:()=>false,remove(){}}];return {querySelectorAll:()=>scripts,querySelector:()=>null};}}
let body=payload,headers={},chunks=null;
const readers=[];
global.fetch=async(url,options)=>{
  assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');
  return {ok:true,redirected:false,headers:new Headers({'Content-Type':'text/html',...headers}),body:{getReader(){
    const stream=new ReadableStream({start(controller){for(const chunk of chunks||[body])controller.enqueue(chunk);controller.close();}});
    const reader=stream.getReader(),cleanup={cancelled:0,released:0};readers.push(cleanup);
    return {read:()=>reader.read(),cancel:async()=>{cleanup.cancelled++;await reader.cancel();},releaseLock(){cleanup.released++;reader.releaseLock();}};
  }}};
};
headers={'Content-Length':String(payload.length)};
assert.equal((await readFull(new AbortController().signal)).data.runs[0].id,'r1');
// Split every byte, including UTF-8 code points, and retain empty chunks.
chunks=Array.from(payload,(_,index)=>payload.subarray(index,index+1));chunks.splice(2,0,new Uint8Array(0));
headers={'Content-Encoding':'gzip','Content-Length':'1'};
assert.equal((await readFull(new AbortController().signal)).data.runs[0].id,'r1');
chunks=null;
headers={'Content-Length':'1'};await assert.rejects(readFull(new AbortController().signal),/viewer length/);
headers={'Content-Encoding':'gzip','Content-Length':'not-numeric'};await assert.rejects(readFull(new AbortController().signal),/viewer length/);
headers={'Content-Encoding':'gzip'};body=new Uint8Array(payload.length+1);await assert.rejects(readFull(new AbortController().signal),/viewer decoded size/);
body=payload.slice(1);await assert.rejects(readFull(new AbortController().signal),/viewer truncated/);
body=new Uint8Array(payload.length);await assert.rejects(readFull(new AbortController().signal),/viewer SHA256/);
body=payload;headers={'Content-Type':'application/javascript'};await assert.rejects(readFull(new AbortController().signal),/viewer response/);
headers={};seed.full.script_sha256='0'.repeat(64);await assert.rejects(readFull(new AbortController().signal),/viewer code SHA256/);
assert.equal(readers.length,6);
for(const cleanup of readers)assert.deepEqual(cleanup,{cancelled:1,released:1});
// An aborted read must propagate the original error and clean up the reader.
seed.full.script_sha256=await digest(new TextEncoder().encode(program));
const controller=new AbortController(),abortError=new DOMException('cancelled stream','AbortError');
const abortCleanup={cancelled:0,released:0};let observedSignal,readStarted;
const reading=new Promise(resolve=>{readStarted=resolve;});
global.fetch=async(url,{signal})=>{
  observedSignal=signal;
  return {ok:true,redirected:false,headers:new Headers({'Content-Type':'text/html'}),body:{getReader(){return {
    read:()=>new Promise((resolve,reject)=>{signal.addEventListener('abort',()=>reject(abortError),{once:true});readStarted();}),
    cancel:async()=>{abortCleanup.cancelled++;},releaseLock(){abortCleanup.released++;}
  };}}};
};
const parsedBeforeAbort=parsed,pending=readFull(controller.signal);
await reading;assert.equal(observedSignal,controller.signal);controller.abort();
await assert.rejects(pending,error=>error===abortError);
assert.deepEqual(abortCleanup,{cancelled:1,released:1});assert.equal(parsed,parsedBeforeAbort);
// Abort before obtaining a response must also propagate, without parsing.
global.fetch=async(url,{signal})=>{assert.equal(signal.aborted,true);throw abortError;};
await assert.rejects(readFull(controller.signal),error=>error===abortError);
assert.equal(parsed,parsedBeforeAbort);
""")


if __name__ == '__main__':
    unittest.main()
