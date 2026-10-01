import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package: root, segments} = JSON.parse(readFileSync(0, 'utf8'));
const {estimateTokens} = await import(pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
console.log(JSON.stringify({counter: 'pi.estimateTokens', counts: segments.map(text =>
    estimateTokens({role:'user', content:[{type:'text', text}], timestamp:0}))}));
