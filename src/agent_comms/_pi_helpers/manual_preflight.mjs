import { pathToFileURL } from 'node:url';
const root = process.env.COMPACT_PI_PACKAGE;
const { SessionManager } = await import(pathToFileURL(root + '/dist/core/session-manager.js'));
const session = SessionManager.open(
  process.env.COMPACT_SESSION, undefined, process.env.COMPACT_CWD);
console.log(JSON.stringify({
  sessionId: session.getSessionId(), sessionFile: session.getSessionFile(),
}));
