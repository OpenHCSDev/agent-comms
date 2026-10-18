/** Real SDK write/edit definitions and local operations; no native/provider launch. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [packageRoot, directory] = process.argv.slice(2);
const {createWriteToolDefinition} = await import(pathToFileURL(join(packageRoot,'dist/core/tools/write.js')));
const {createEditToolDefinition} = await import(pathToFileURL(join(packageRoot,'dist/core/tools/edit.js')));
const {CompletedFileMutation} = await import(pathToFileURL(join(packageRoot,'dist/core/tools/agent-comms-file-artifact.js')));
const {nativeFileArtifactSchema: schema} = await import(pathToFileURL(join(packageRoot,'dist/core/tools/native-file-artifact-schema.mjs')));
await mkdir(directory,{recursive:true,mode:0o700});
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const original = '\ufeffvalue = "\u03bb"\r\n';
const write = createWriteToolDefinition(directory);
const edit = createEditToolDefinition(directory);
const first = await write.execute('original-write',{path:'artifact.py',content:original});
const firstBytes = await readFile(join(directory,'artifact.py'));
assert.equal(first.details[schema.discriminator],schema.details_name);
assert.equal(first.details[schema.artifact_field].digest.value,hash(firstBytes));
assert.equal(first.details[schema.artifact_field].byte_count,firstBytes.length);
assert.equal(first.details[schema.artifact_field].operation_path,join(directory,'artifact.py'));
const second = await edit.execute('original-edit',{path:'artifact.py',edits:[{
    oldText:'value = "\u03bb"',newText:'value = "changed"'}]});
const editedBytes = await readFile(join(directory,'artifact.py'));
assert.equal(editedBytes.toString(),'\ufeffvalue = "changed"\r\n');
assert.equal(second.details[schema.artifact_field].digest.value,hash(editedBytes));
assert.notEqual(first.details[schema.artifact_field].digest.value,hash(editedBytes));
assert.ok(second.details.patch.includes('value = "changed"'));
await assert.rejects(edit.execute('failed-edit',{path:'artifact.py',edits:[{
    oldText:'not the original source',newText:'bad'}]}));
assert.deepEqual(await readFile(join(directory,'artifact.py')),editedBytes);

// Custom SDK operations remain a genuine optional external contract. They
// cannot acquire our local-operation receipt merely by resolving successfully.
const custom = createWriteToolDefinition(directory,{operations:{
    mkdir: path=>mkdir(path,{recursive:true}).then(()=>{}),
    writeFile: (path,content)=>writeFile(path,content+' custom transformation','utf8'),
}});
const customResult = await custom.execute('custom-write',{path:'custom.py',content:'requested'});
assert.equal(customResult.details,undefined);
assert.equal((await readFile(join(directory,'custom.py'))).toString(),'requested custom transformation');

// The existing SDK cancellation check runs AFTER the original filesystem
// operation. Its side effect survives, but no successful ToolResult is emitted.
const controller = new AbortController();
const cancelled = createWriteToolDefinition(directory,{operations:{
    mkdir: path=>mkdir(path,{recursive:true}).then(()=>{}),
    writeFile: async(path,content)=>{
        const completed = await CompletedFileMutation.write(path,content);
        controller.abort();
        return completed;
    },
}});
await assert.rejects(cancelled.execute('cancelled-write',{
    path:'cancelled.py',content:'original uncertain side effect'},controller.signal),/Operation aborted/);
assert.equal((await readFile(join(directory,'cancelled.py'))).toString(),'original uncertain side effect');
console.log(JSON.stringify({state:'ACTUAL_SDK_LOCAL_OPERATION_SEMANTICS_PASS',
    original_write:first, original_edit:second, custom_result:customResult,
    failed_edit_result_emitted:false,cancelled_result_emitted:false,
    cancelled_side_effect_preserved:true,new_native_processes:0,provider_calls:0}));
