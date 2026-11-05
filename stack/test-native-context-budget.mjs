#!/usr/bin/env node
// The context clamp must leave room for the estimator's measured error.
import assert from 'node:assert/strict';
import {join} from 'node:path';

const pkg=process.env.PI_NATIVE_PACKAGE_DIR;
assert.ok(pkg,'Matched installed native bundle required');
const {clampMaxTokensToContext}=await import(
    join(pkg,'node_modules/@earendil-works/pi-ai/dist/api/simple-options.js'));

const contextWindow=1048576;
const estimated=144157;
const actual=151335;
const context={messages:[{role:'user',content:'x'.repeat(estimated*4)}]};
const maxTokens=clampMaxTokensToContext({contextWindow},context,943718);
assert.equal(maxTokens,contextWindow-estimated-Math.ceil(estimated*0.1));
assert.ok(actual+maxTokens<=contextWindow,`${actual}+${maxTokens} exceeds ${contextWindow}`);

const smallWindow=20000;
const floor=clampMaxTokensToContext({contextWindow:smallWindow},
    {messages:[{role:'user',content:'x'.repeat(40000)}]},943718);
assert.equal(floor,smallWindow-10000-4096);

console.log(JSON.stringify({contextWindow,estimatedInputTokens:estimated,actualInputTokens:actual,
    maxTokens,headroom:contextWindow-actual-maxTokens,floor}));
