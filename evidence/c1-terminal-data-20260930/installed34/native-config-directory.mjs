import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {getAgentDir} = await import(pathToFileURL(join(process.argv[1],'dist/config.js')));
console.log(JSON.stringify({agentDir:getAgentDir()}));
