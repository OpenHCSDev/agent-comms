import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

// This is the single native boundary for Python's declared launch family.
const nameOf = (type, suffix) => type.name.slice(0, -suffix.length)
  .replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
function decode(members, suffix, wire) {
  const declarations = new Map(members.map(type => [nameOf(type, suffix), type]));
  const Type = declarations.get(wire.kind);
  if (!Type) throw new Error(`Unknown ${suffix}: ${wire.kind}`);
  return Object.freeze(new Type(wire));
}
class NativeOriginPolicy {
  constructor(wire) { Object.assign(this, wire); }
  apply(operation) {}
}
class UnrestrictedNativeOriginPolicy extends NativeOriginPolicy {}
class LoopbackNativeOriginPolicy extends NativeOriginPolicy {
  apply(operation) { operation(this.value); }
}
class LaunchRecord {
  constructor(wire) {
    Object.assign(this, wire);
    this.origin = decode([UnrestrictedNativeOriginPolicy, LoopbackNativeOriginPolicy],
      'NativeOriginPolicy', wire.origin);
  }
  get managed() { return false; }
  currentThread() {}
  withProject(operation) {}
  withOrigin(operation) { this.origin.apply(operation); }
  runChild(program, args, options) {
    const stdio = Array.from({length: Math.max(descriptor + 1, 3)}, () => 'ignore');
    stdio[0] = 'pipe'; stdio[1] = 'pipe'; stdio[2] = 'pipe';
    stdio[descriptor] = descriptor;
    return execFileSync(program, ['--comms-launch', path, ...args], {...options, stdio});
  }
}
class CommandLineLaunchRecord extends LaunchRecord {}
class AcpLaunchRecord extends LaunchRecord {}
class RestartQueueLaunchRecord extends LaunchRecord {}
class LoginLaunchRecord extends LaunchRecord {}
class OwnerLaunchRecord extends LaunchRecord {
  currentThread() { return this.thread.name; }
}
class NativeOwnerLaunchRecord extends OwnerLaunchRecord {
  get managed() { return true; }
}
class ProjectLaunchRecord extends NativeOwnerLaunchRecord {
  withProject(operation) { operation(this.project_socket, this.project_request); }
}
const index = process.argv.indexOf('--comms-launch');
if (index < 0 || index + 1 >= process.argv.length)
  throw new Error('An explicit --comms-launch FILE is required');
const path = process.argv[index + 1];
const descriptor = Number(path.split('/').at(-1));
if (!Number.isInteger(descriptor)) throw new Error('Launch requires an inherited descriptor');
process.argv.splice(index, 2);
export const launchRecord = decode([
  CommandLineLaunchRecord, OwnerLaunchRecord, NativeOwnerLaunchRecord, ProjectLaunchRecord,
  LoginLaunchRecord, AcpLaunchRecord, RestartQueueLaunchRecord,
], 'LaunchRecord', JSON.parse(fs.readFileSync(path, 'utf8')));
globalThis.agentCommsLaunchRecord = launchRecord;
