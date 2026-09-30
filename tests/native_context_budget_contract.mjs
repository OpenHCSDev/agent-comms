/** Actual provider adapters with a local endpoint; no native/UI replacement. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const [pkg, imageFile, receiptFile] = process.argv.slice(2);
const api=join(pkg,'node_modules/@earendil-works/pi-ai/dist/api');
const {ContextBudget,ContextBudgetRequest,ProviderRejection,BudgetAdmissionError}=await import(pathToFileURL(join(api,'agent-comms-context-budget.js')));
const {buildBaseOptions}=await import(pathToFileURL(join(api,'simple-options.js')));
const {chatInput,anthropicInput,googleInput,bedrockInput,responsesInput}=await import(pathToFileURL(join(api,'agent-comms-request-input.js')));
const {estimateContextTokens,estimateSerializedRequestTokens}=await import(pathToFileURL(join(pkg,'node_modules/@earendil-works/pi-ai/dist/utils/estimate.js')));
const chat=await import(pathToFileURL(join(api,'openai-completions.js')));
const anthropic=await import(pathToFileURL(join(api,'anthropic-messages.js')));
const imageBytes=readFileSync(imageFile),data=imageBytes.toString('base64');
assert(imageBytes.length>8*1024*1024);
const context={messages:[{role:'user',content:[{type:'text',text:'describe'},{type:'image',mimeType:'image/png',data}],timestamp:1}],tools:[]};
const capability={contextWindow:1048575,maxTokens:943717};
assert.equal(buildBaseOptions(capability,context,{},'local').maxTokens,undefined);
assert.equal(buildBaseOptions(capability,context,{maxTokens:1000000},'local').maxTokens,943717);
assert.equal(new ContextBudget({...capability,maxTokens:131072},context).allowance(943717),131072);
const schemas=[
 chatInput({messages:[{content:[{type:'text',text:'describe'},{type:'image_url',image_url:{url:'data:image/png;base64,'+data}}]}]}),
 anthropicInput({messages:[{content:[{type:'text',text:'describe'},{type:'image',source:{type:'base64',media_type:'image/png',data}}]}]}),
 responsesInput({input:[{type:'message',role:'user',content:[{type:'input_text',text:'describe'},{type:'input_image',image_url:'data:image/png;base64,'+data}]}]}),
 googleInput({contents:[{parts:[{text:'describe'},{inlineData:{mimeType:'image/png',data}}]}],config:{}}),
 bedrockInput({messages:[{content:[{text:'describe'},{image:{format:'png',source:{bytes:imageBytes}}}]}]}),
];
const nativeEstimate=estimateContextTokens(context).tokens;
for (const input of schemas) assert.equal(estimateSerializedRequestTokens(context,input),nativeEstimate);
let requests=[];
const server=createServer(async(req,res)=>{
 const chunks=[];for await(const block of req)chunks.push(block);
 const raw=Buffer.concat(chunks),payload=JSON.parse(raw);
 requests.push({url:req.url,bytes:raw.length,max_tokens:payload.max_tokens,tools:payload.tools?.length??0});
 res.writeHead(200,{'content-type':'text/event-stream'});
 if(req.url.startsWith('/v1/messages')) {
  for(const event of [
   {type:'message_start',message:{id:'fixture',type:'message',role:'assistant',content:[],model:'fixture',usage:{input_tokens:1202,output_tokens:0}}},
   {type:'content_block_start',index:0,content_block:{type:'text',text:''}},
   {type:'content_block_delta',index:0,delta:{type:'text_delta',text:'IMAGE_BUDGET_OK'}},
   {type:'content_block_stop',index:0},
   {type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:4}},
   {type:'message_stop'},
  ])res.write('event: '+event.type+'\ndata: '+JSON.stringify(event)+'\n\n');
 } else {
  for(const event of [
   {id:'fixture',object:'chat.completion.chunk',created:1,model:'fixture',choices:[{index:0,delta:{role:'assistant',content:'IMAGE_BUDGET_OK'},finish_reason:null}]},
   {id:'fixture',object:'chat.completion.chunk',created:1,model:'fixture',choices:[{index:0,delta:{},finish_reason:'stop'}],usage:{prompt_tokens:1202,completion_tokens:4,total_tokens:1206}},
  ])res.write('data: '+JSON.stringify(event)+'\n\n');
  res.write('data: [DONE]\n\n');
 }
 res.end();
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const model={id:'fixture',name:'Fixture',provider:'selected-offline',api:'openai-completions',baseUrl:`http://127.0.0.1:${server.address().port}/v1`,contextWindow:8192,maxTokens:1024,reasoning:false,input:['text','image'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},compat:{maxTokensField:'max_tokens'}};
const options={apiKey:'local-fixture',maxRetries:0};
try {
 const absent=await chat.streamSimple(model,context,options).result();assert.equal(absent.stopReason,'stop',absent.errorMessage);
 assert.equal(requests[0].max_tokens,undefined);assert(requests[0].bytes>8*1024*1024);
 const explicit=await chat.streamSimple(model,context,{...options,maxTokens:1000000,onPayload:p=>({...p,max_tokens:1000000,tools:[{type:'function',function:{name:'actual-tool',description:'t'.repeat(4000),parameters:{type:'object',properties:{}}}}]})}).result();
 assert.equal(explicit.stopReason,'stop',explicit.errorMessage);assert.equal(requests[1].max_tokens,1024);assert.equal(requests[1].tools,1);
 const nonfit=await chat.streamSimple(model,context,{...options,onPayload:p=>({...p,messages:[...p.messages,{role:'user',content:'x'.repeat(40000)}]})}).result();
 assert.equal(nonfit.stopReason,'error');assert.match(nonfit.errorMessage,/no admissible generation/);assert.equal(requests.length,2);
 const mandatory=await anthropic.streamSimple({...model,api:'anthropic-messages',baseUrl:model.baseUrl.slice(0,-3)},context,{...options,onPayload:p=>({...p,max_tokens:1000000})}).result();
 assert.equal(mandatory.stopReason,'stop',mandatory.errorMessage);assert.equal(requests[2].max_tokens,1024);assert(requests[2].bytes>8*1024*1024);
 // Same rejection family, old grammar, and correspondence/malformed guards.
 const small={messages:[{role:'user',content:[{type:'text',text:'x'}],timestamp:1}]};
 const error=(message)=>Object.assign(new Error(message),{status:400,error:{message}});
 const original=error("This endpoint's maximum context length is 1048576 tokens. However, you requested about 1076118 tokens (427363 of text input, 5273 of tool input, 643482 in the output)");
 const request=new ContextBudgetRequest(capability,small,{max_tokens:643482},'max_tokens',async()=>{},small);
 assert(ProviderRejection.decode(original).revisedAllowance(request)>0);
 request.params.max_tokens=2;assert.equal(ProviderRejection.decode(original).revisedAllowance(request),undefined);
 for(const rejection of [error('Unsupported parameter'),error("This endpoint's maximum context length is 10 tokens. However, you requested about 20 tokens (10 of text input, 1 of tool input, 12 in the output)")])assert.equal(ProviderRejection.decode(rejection).revisedAllowance(request),undefined);
 const old=error('maximum context length of 1048576 tokens. You requested a total of 1076118 tokens: 432636 tokens from the input messages and 643482 tokens for the completion');
 request.params.max_tokens=643482;assert(ProviderRejection.decode(old).revisedAllowance(request)>0);
 const abort=new AbortController();let sends=0;
 const cancelled=new ContextBudgetRequest(capability,small,{max_tokens:643482},'max_tokens',async()=>{sends++;abort.abort();throw original;},small,abort.signal);
 await assert.rejects(cancelled.send());assert.equal(sends,1);
 const receipt={actual_image_bytes:imageBytes.length,canonical_image_estimate:nativeEstimate,external_schema_image_projections:5,actual_provider_adapters:['openai-completions','anthropic-messages'],requests,desired_absence:true,explicit_caller_and_final_hook_cap:true,final_tool_schema:true,nonfit_http_calls:0,mandatory_anthropic:true,secondary_rejection_guards:true,cancel_during_rejection_calls:1,paid_calls:0,limits:'Provider adapter/local HTTP contracts, not eleven APIs installed acceptance. Native/ACP/original retained journey separately required.'};
 writeFileSync(receiptFile,JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify(receipt));
}finally{await new Promise(resolve=>server.close(resolve));}
