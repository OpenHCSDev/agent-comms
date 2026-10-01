import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [packageRoot, privateConfig, provider, modelId] = process.argv.slice(1);
globalThis.fetch = () => { throw Error('Model capability observation permits no network'); };
const {ModelRuntime} = await import(pathToFileURL(join(packageRoot, 'dist/core/model-runtime.js')));
const runtime = await ModelRuntime.create({
  authPath: join(privateConfig,'auth.json'),
  modelsPath: join(privateConfig,'models.json'),
  modelsStorePath: join(privateConfig,'models-store.json'),
  refreshOnCreate: false, allowModelNetwork: false,
});
// The original Models catalog owner restores only the selected cached catalog.
// No runtime availability/auth refresh, stream or completion is requested.
const refreshed = await runtime.models.refresh({providers:[provider],allowNetwork:false});
if (refreshed?.errors?.size) throw Error('Selected cached model catalog could not be observed');
const model = runtime.getModel(provider,modelId);
if (!model || !(model.contextWindow>0) || !(model.maxTokens>0)) throw Error('Original SDK selected Model capabilities unavailable');
console.log(JSON.stringify({provider:model.provider,id:model.id,api:model.api,contextWindow:model.contextWindow,maxTokens:model.maxTokens,
  source:'SDK ModelRuntime.create + original Models.refresh(allowNetwork:false) + ModelRuntime.getModel',networkRequests:0}));
