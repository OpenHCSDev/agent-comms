import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const packageRoot = resolve(process.env.AC_NATIVE_HISTORY_PACKAGE);
const moduleAt = (name) => import(pathToFileURL(join(packageRoot, 'dist', name)));
const {SessionManager} = await moduleAt('core/session-manager.js');
const {collectEntriesForBranchSummary, prepareBranchEntries} = await moduleAt('core/compaction/branch-summarization.js');
const {exportFromFile, exportSessionToHtml} = await moduleAt('core/export-html/index.js');
const {collectCacheMisses, computeCacheWaste} = await moduleAt('core/cache-stats.js');

test('native branch summary consumes the store range and preserves its budget', () => {
    const manager = SessionManager.inMemory(process.cwd());
    const root = manager.appendMessage({role:'user',content:'root',timestamp:1});
    const left = manager.appendMessage({role:'user',content:'left',timestamp:2});
    const tip = manager.appendMessage({role:'user',content:'tip',timestamp:3});
    manager.branch(root);
    const right = manager.appendMessage({role:'user',content:'right',timestamp:4});
    const {entries,commonAncestorId} = collectEntriesForBranchSummary(manager,tip,right);
    assert.equal(commonAncestorId,root);
    assert.deepEqual([...entries].map(entry=>entry.id),[left,tip]);
    assert.deepEqual([...entries.reverse()].map(entry=>entry.id),[tip,left]);
    assert.deepEqual(prepareBranchEntries(entries,1000).messages.map(message=>message.content),['left','tip']);
    assert.equal(collectEntriesForBranchSummary(manager,null,right).entries.isEmpty(),true);
    assert.equal(collectEntriesForBranchSummary(manager,root,tip).entries.isEmpty(),true);
    manager.appendLabelChange(root, 'retired label');
    const labelled = manager.getLeafId();
    manager.appendLabelChange(root, undefined);
    manager.createBranchedSession(labelled);
    assert.equal(manager.getLabel(root), undefined, 'fork preserves a later label clearing');
    manager.entryStore.close();
});

test('HTML exports full retained entries and custom rendering without consuming history twice', async () => {
    const scratch = resolve(process.env.TMPDIR ?? '.artifacts');
    mkdirSync(scratch,{recursive:true});
    const directory=mkdtempSync(join(scratch,'native-html-'));
    const session=join(directory,'session.jsonl');
    const header={type:'session',version:3,id:'export-test',cwd:directory,timestamp:'2026-09-28T17:00:00Z'};
    const rows=[
        {type:'message',id:'a',parentId:null,message:{role:'user',content:'multibyte 🐸 日本語'}},
        {type:'message',id:'b',parentId:'a',message:{role:'assistant',content:[{type:'toolCall',id:'tool',name:'custom',arguments:{path:'🐸'}}]}},
        {type:'message',id:'c',parentId:'b',message:{role:'toolResult',toolCallId:'tool',toolName:'custom',content:[{type:'text',text:'success'}]}},
    ];
    writeFileSync(session,[header,...rows].map(row=>JSON.stringify(row)+'\n').join(''));
    const original=readFileSync(session);
    const decode=(path)=>JSON.parse(Buffer.from(readFileSync(path,'utf8').match(/<script id="session-data" type="application\/json">([^<]*)<\/script>/)[1],'base64').toString());
    try {
        const plain=join(directory,'plain.html');
        await exportFromFile(session,{outputPath:plain});
        assert.deepEqual(decode(plain).entries,rows);
        const manager=SessionManager.open(session);
        try {
            const rendered=join(directory,'rendered.html');
            await exportSessionToHtml(manager,{}, {outputPath:rendered,toolRenderer:{
                renderCall:()=>'<b>call 🐸</b>',renderResult:()=>({collapsed:'small',expanded:'full'}),
            }});
            assert.deepEqual(decode(rendered).entries,rows);
            assert.deepEqual(decode(rendered).renderedTools.tool,{callHtml:'<b>call 🐸</b>',resultHtmlCollapsed:'small',resultHtmlExpanded:'full'});
            await assert.rejects(exportSessionToHtml(manager,{}, {outputPath:session}),/overwrite session/);
            assert.deepEqual(readFileSync(session),original);
        } finally {manager.entryStore.close();}
    } finally {rmSync(directory,{recursive:true,force:true});}
});

test('cache notices use persisted identity when disk reads return distinct message objects', () => {
    const usage={input:5000,output:1,cacheRead:1,cacheWrite:0,cost:{input:1,cacheRead:0,cacheWrite:0}};
    const rows=[1,2].map(id=>({type:'message',id:String(id),message:{role:'assistant',provider:'local',model:'fixture',timestamp:id,usage}}));
    const models={getModel:()=>({cost:{cacheRead:0}})};
    const misses=collectCacheMisses(rows.values(),models);
    assert.equal(misses.has('2'),true);
    assert.equal(misses.has(rows[1].message),false);
    assert.equal(computeCacheWaste(rows.values(),models).missCount,1);
});
