/** Actual provider adapters against a local HTTP fixture: request progress stages.
 * Diagnostic producer proof only; no installed ACP/owner or live provider claim.
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const [pkg, receiptPath] = process.argv.slice(2);
const dist = join(pkg, 'node_modules/@earendil-works/pi-ai/dist');
const load = path => import(pathToFileURL(join(dist, path)));
const chat = await load('api/openai-completions.js');
const anthropic = await load('api/anthropic-messages.js');
const codex = await load('api/openai-codex-responses.js');
const {NativeRequestObservation, observeStream} = await load('utils/agent-comms-request-observation.js');
const {WebSocketServer} = createRequire(pathToFileURL(join(pkg, 'entry.cjs')))('ws');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const requests = [];
let mode = 'ordinary', responses = 0;
const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const payload = JSON.parse(Buffer.concat(chunks));
    requests.push({mode, requested: payload.max_tokens, messages: JSON.stringify(payload.messages), at: performance.now()});
    responses++;
    if (mode === 'transient' && responses === 1) {
        res.writeHead(503, {'content-type':'application/json','retry-after':'0'});
        res.end(JSON.stringify({error:{message:'Service unavailable'}})); return;
    }
    await sleep(30);
    res.writeHead(200, {'content-type':'text/event-stream'}); res.flushHeaders();
    await sleep(30);
    if (req.url.startsWith('/v1/messages')) {
        const events = [
            {type:'message_start',message:{id:'local',model:'fixture',role:'assistant',content:[],usage:{input_tokens:1,output_tokens:0}}},
            {type:'content_block_start',index:0,content_block:{type:'text',text:''}},
            {type:'content_block_delta',index:0,delta:{type:'text_delta',text:'OBSERVED'}},
            {type:'content_block_stop',index:0},
            {type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:1}},
            {type:'message_stop'},
        ];
        for (const event of events) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    } else {
        // Metadata arrives before text: first_event must not wait for text_delta.
        const event = {id:'local',model:'fixture',choices:[{index:0,delta:{role:'assistant'},finish_reason:null}]};
        res.write(`data: ${JSON.stringify(event)}\n\n`);
        await sleep(15);
        for (const delta of [{content:'OBSERVED'},{}]) {
            event.choices[0].delta = delta;
            event.choices[0].finish_reason = Object.keys(delta).length ? null : 'stop';
            res.write(`data: ${JSON.stringify(event)}\n\n`);
        }
        res.write('data: [DONE]\n\n');
    }
    res.end();
});
const wss = new WebSocketServer({server});
let wsSends = 0;
wss.on('connection', socket => socket.on('message', async () => {
    wsSends++;
    await sleep(20);
    socket.send(JSON.stringify({type:'response.created',response:{id:'ws-local',status:'in_progress'}}));
    await sleep(20);
    socket.send(JSON.stringify({type:'response.completed',response:{id:'ws-local',status:'completed',output:[],usage:{input_tokens:1,output_tokens:1,total_tokens:2}}}));
}));
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
const baseUrl = `http://127.0.0.1:${server.address().port}`;
const model = {id:'fixture',name:'Fixture',provider:'local',api:'openai-completions',baseUrl:baseUrl+'/v1',contextWindow:8192,maxTokens:1024,reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},compat:{maxTokensField:'max_tokens'}};
const context = {messages:[{role:'user',inputId:'private-control',content:[{type:'text',text:'x'}],timestamp:1}]};
const controls = [];
async function run(label, adapter, selectedModel, config = {}) {
    mode = label; responses = 0;
    const points = [], nativeEvents = [];
    const observation = new NativeRequestObservation({sessionId:'private-control',onRequestProgress:p=>{
        points.push(p);
        if (label==='throwing') throw new Error('Observer failure');
        if (label==='cancel' && p.stage==='first_event') config.signalOwner.abort();
    }},context);
    const start = requests.length;
    const options = observation.options({apiKey:'local-fixture',maxRetries:0,
        ...config, onResponse:async()=>{ if(label==='ordinary') await sleep(120); }});
    const stream = adapter.streamSimple(selectedModel,context,options);
    for await (const event of stream) nativeEvents.push({type:event.type,at:performance.now()});
    const result = await stream.result();
    const selected = points.filter(p=>['dispatch','headers','first_event','stream_end'].includes(p.stage));
    const record = {label,stopReason:result.stopReason,points:selected,nativeEvents,
        posts:requests.length-start,callbackMs:points.at(-1)?.callbackMs};
    if(label==='cancel') assert.equal(result.stopReason,'aborted',result.errorMessage);
    else assert.equal(result.stopReason,'stop',result.errorMessage);
    assert.equal(points.filter(p=>p.stage==='stream_end').length,1,label);
    assert.equal(points.filter(p=>p.stage==='first_event').length,1,label);
    assert.equal(new Set(points.map(p=>p.requestId)).size,1);
    assert(points.every(p=>p.inputId==='private-control' && p.sessionId==='private-control'));
    if(label==='ordinary') {
        const headers = points.find(p=>p.stage==='headers');
        const first = points.find(p=>p.stage==='first_event');
        assert(first.elapsedMs-headers.elapsedMs>=110);
        assert(first.callbackMs>=110);
        assert.deepEqual(selected.map(p=>p.stage),['dispatch','headers','first_event','stream_end']);
    }
    if(label==='transient') assert.deepEqual(selected.filter(p=>p.stage==='dispatch').map(p=>p.attempt),[0,1]);
    controls.push(record);
}
try {
    await run('ordinary',chat,model);
    await run('anthropic',anthropic,{...model,api:'anthropic-messages',baseUrl});
    await run('transient',chat,model,{maxRetries:1});
    await run('throwing',chat,model);
    const signalOwner = new AbortController();
    await run('cancel',chat,model,{signal:signalOwner.signal,signalOwner});
    const token = 'local.'+Buffer.from(JSON.stringify({'https://api.openai.com/auth':{chatgpt_account_id:'local-fixture'}})).toString('base64url')+'.local';
    await run('websocket',codex,{...model,api:'openai-codex-responses',baseUrl}, {apiKey:token,transport:'websocket'});
    const ws = controls.at(-1);
    assert(ws.points.every(p=>p.transport==='websocket'));
    assert(!ws.points.some(p=>p.stage==='headers'));
    assert.equal(wsSends,1);
    // Iterator return must close the upstream resource on early consumption end.
    let released = false; const terminal = [];
    async function* original() {try{yield 1;yield 2;}finally{released=true;}}
    for await (const value of observeStream({onRequestProgress:p=>terminal.push(p)},original(),'http')) {assert.equal(value,1);break;}
    assert(released); assert.deepEqual(terminal.map(p=>p.stage),['first_event','stream_end']);
    const receipt={controls,iterator_return_releases_original:true,ws_requests:wsSends,paid_calls:0,
        limits:'Actual provider adapters and localhost HTTP/WebSocket only; no installed ACP/native full workflow or live provider wait attribution. Candidate uses original454 retry counter seam; no new retry authority.'};
    writeFileSync(receiptPath,JSON.stringify(receipt,null,2)+'\n');
    console.log(JSON.stringify({controls:controls.map(c=>({label:c.label,posts:c.posts,stopReason:c.stopReason})),paid_calls:0}));
} finally {
    for (const client of wss.clients) client.terminate();
    await new Promise(resolve=>wss.close(resolve));
    server.closeAllConnections(); await new Promise(resolve=>server.close(resolve));
}
