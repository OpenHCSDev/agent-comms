/** Use the registry's project when a managed Pi process resumes a transcript.
 * Pi's CLI otherwise prefers the original cwd in the immutable session header.
 * Its SDK already supports cwdOverride; apply that supported option at CLI entry.
 */
import { realpathSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

if (process.env.AGENT_COMMS_MANAGED === "1" && process.env.PI_WORKTREE && process.argv[1]) {
  let entry;
  try { entry = realpathSync(process.argv[1]); } catch { /* node stdin / non-file entry */ }
  const bundled = entry && basename(dirname(entry)) === "bundle";
  const dist = entry ? (bundled ? dirname(dirname(entry)) : dirname(entry)) : "";
  // NODE_OPTIONS may be inherited by tool subprocesses. Only adjust Pi's CLI.
  if (entry && basename(entry) === "cli.js" && basename(dist) === "dist" && basename(dirname(dist)) === "pi-coding-agent") {
    const { SessionManager } = await import(pathToFileURL(join(dist, "core/session-manager.js")).href);
    const open = SessionManager.open;
    SessionManager.open = function (path, sessionDir, cwdOverride) {
      return open.call(this, path, sessionDir, cwdOverride ?? process.env.PI_WORKTREE);
    };
    if (bundled) {
      // The bundled CLI has its own private SessionManager copy. Run the
      // distributed unbundled CLI so the SDK override applies to that runtime.
      const { setupCli } = await import(pathToFileURL(join(dist, "cli/setup.js")).href);
      const { main } = await import(pathToFileURL(join(dist, "main.js")).href);
      process.argv[1] = join(dist, "cli.js");
      setupCli();
      await main(process.argv.slice(2));
      process.exit(process.exitCode ?? 0);
    }
  }
}
