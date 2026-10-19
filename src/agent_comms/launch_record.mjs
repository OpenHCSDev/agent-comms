import fs from 'node:fs';

const index = process.argv.indexOf('--comms-launch');
if (index < 0 || index + 1 >= process.argv.length)
  throw new Error('An explicit --comms-launch FILE is required');
const path = process.argv[index + 1];
process.argv.splice(index, 2);
// Python's LaunchRecord codec produced this private file. Node decodes it once,
// before loading the SDK or any extension; every consumer shares this object.
export const launchRecord = Object.freeze(JSON.parse(fs.readFileSync(path, 'utf8')));
globalThis.agentCommsLaunchRecord = launchRecord;
