const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../../app/static/onair/special-events.js'),'utf8');
function rig({native=false,mismatch=false}={}){
 const nodes=new Map(),calls=[];let saved=null;
 function node(id){if(!nodes.has(id))nodes.set(id,{value:'',checked:true,innerHTML:'',textContent:'',dataset:{},handlers:{},files:[],click(){this.clicked=true;},reset(){},addEventListener(name,fn){this.handlers[name]=fn;}});return nodes.get(id);}
 const window={crypto:{randomUUID:()=> 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}};if(native)window.chrome={webview:{}};
 const api=async(url,opt={})=>{calls.push({url,opt});if(url.includes('preview-folder'))return{complete:true,tracks:[{path:'C:/Downloads/song.wav',title:'Song',duration:0}]};if(url.includes('/upload'))return{track:{path:'/imported/'+opt.body.file.name,title:opt.body.file.name.replace('.wav',''),artist:'',duration:0}};if(opt.method==='POST'){saved={...JSON.parse(opt.body),id:'saved',track_count:2};return{program:saved};}return{programs:saved?[mismatch?{...saved,name:'wrong'}:saved]:[]};};
 class FormData{append(name,file){this.file=file;}}
 const context={window,document:{getElementById:node},Date,FormData,console};vm.runInNewContext(source,context);
 window.RadioSpecialEvents.mount({api,escapeHtml:s=>String(s).replaceAll('<','&lt;'),getStations:()=>[{id:1,name:'Radio'}],pickOperatorPath:async()=>({selected:true,folder:'C:/Downloads'})});
 return{nodes,node,calls,window,api};
}
async function setup(r){await r.window.RadioSpecialEvents.load(1);const vals={eventName:'29 Ekim',eventMode:'playlist',eventStartDate:'2026-10-29',eventEndDate:'2026-10-29',eventStartTime:'09:00',eventEndTime:'17:00',eventTimezone:'Europe/Istanbul',eventPriority:'100',eventOrder:'ordered',eventBoundary:'exact',eventCrossfade:'3',eventMetadata:'Cumhuriyet Bayramı'};Object.entries(vals).forEach(([k,v])=>r.node(k).value=v);}
test('browser opens Windows folder chooser without requesting a session-zero server dialog',async()=>{const r=rig();await setup(r);await r.node('eventPickFolder').handlers.click();assert.equal(r.node('eventFolderFiles').clicked,true);assert.equal(r.calls.filter(c=>c.url.includes('pick-folder')).length,0);});
test('desktop folder selection previews the actual directory',async()=>{const r=rig({native:true});await setup(r);await r.node('eventNativeFolder').handlers.click();await new Promise(resolve=>setImmediate(resolve));assert.equal(r.node('eventFolder').value,'C:/Downloads');assert.ok(r.calls.some(c=>c.url.includes('preview-folder')));});
test('drag order, exact selected bytes and read-back are retained in the saved plan',async()=>{const r=rig();await setup(r);r.node('eventFolderFiles').files=[{name:'a.wav',size:5,webkitRelativePath:'Holiday/a.wav'},{name:'b.wav',size:5,webkitRelativePath:'Holiday/b.wav'}];r.node('eventFolderFiles').handlers.change();const dt={setData(){}};r.node('eventTracks').handlers.dragstart({target:{closest:()=>({dataset:{eventTrack:'0'}})},dataTransfer:dt});r.node('eventTracks').handlers.drop({preventDefault(){},target:{closest:()=>({dataset:{eventTrack:'1'}})}});await r.node('eventForm').handlers.submit({preventDefault(){}});const posted=r.calls.find(c=>c.opt.method==='POST'&&c.url.endsWith('special-events'));assert.ok(posted);const payload=JSON.parse(posted.opt.body);assert.deepEqual(payload.tracks.map(t=>t.title),['b','a']);assert.equal(payload.metadata_title,'Cumhuriyet Bayramı');assert.equal(payload.client_id,'aaaaaaaabbbbccccddddeeeeeeeeeeee');assert.match(r.node('eventResult').textContent,/Kaydedildi ve doğrulandı/);});
test('a differing durable read-back never reports success',async()=>{const r=rig({mismatch:true});await setup(r);r.node('eventMode').value='metadata';await r.node('eventForm').handlers.submit({preventDefault(){}});assert.match(r.node('eventResult').textContent,/doğrulanamadı/);assert.match(r.node('eventResult').className,/error/);});

test('new plans and form reset always default to waiting for the current song',async()=>{const r=rig();await r.window.RadioSpecialEvents.load(1);assert.equal(r.node('eventBoundary').value,'boundary');await setup(r);r.node('eventNew').handlers.click();assert.equal(r.node('eventBoundary').value,'boundary');});

test('boundary choice is persisted and verified without changing to exact takeover',async()=>{const r=rig();await setup(r);r.node('eventBoundary').value='boundary';r.node('eventFolderFiles').files=[{name:'programme.wav',size:5,webkitRelativePath:'Programme/programme.wav'}];r.node('eventFolderFiles').handlers.change();await r.node('eventForm').handlers.submit({preventDefault(){}});const post=r.calls.find(c=>c.opt.method==='POST'&&c.url.endsWith('special-events'));assert.equal(JSON.parse(post.opt.body).start_policy,'boundary');assert.match(r.node('eventResult').textContent,/Kaydedildi ve doğrulandı/);});

test('default HTML explains late start and warns that exact takeover can interrupt',()=>{const html=fs.readFileSync(path.resolve(__dirname,'../../app/static/onair/index.html'),'utf8');assert.match(html,/<option value="boundary" selected>Çalan şarkı bitince/);assert.match(html,/09:02/);assert.match(html,/Saatinde devral \(çalan şarkıyı kesebilir\)/);});
