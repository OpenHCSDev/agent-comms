import { pathToFileURL } from 'node:url';
const {package:root, file} = JSON.parse(process.argv[1]);
const { SessionManager } = await import(pathToFileURL(root + '/dist/core/session-manager.js'));
const session = SessionManager.open(
  file, undefined, process.cwd());
console.log(JSON.stringify({
  sessionId: session.getSessionId(), sessionFile: session.getSessionFile(),
}));
