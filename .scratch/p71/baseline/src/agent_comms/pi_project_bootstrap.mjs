/** Use the registry's project when a managed Pi process resumes a transcript.
 * Pi's CLI otherwise prefers the original cwd in the immutable session header.
 * Its SDK already supports cwdOverride; apply that supported option at CLI entry.
 */
import { realpathSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

// This optional launch policy guards the SDK's default fetch transport, not
// arbitrary sockets or subprocesses. Install before importing any SDK module.
if (process.env.AGENT_COMMS_NATIVE_ORIGIN !== undefined) {
  const value = process.env.AGENT_COMMS_NATIVE_ORIGIN;
  let selected;
  try { selected = new URL(value); } catch { /* rejected below */ }
  if (!selected || selected.protocol !== "http:" ||
      !["127.0.0.1", "[::1]"].includes(selected.hostname) ||
      selected.origin !== value || selected.username || selected.password) {
    const error = new Error("AGENT_COMMS_NATIVE_ORIGIN requires a canonical numeric loopback HTTP origin");
    error.code = "ERR_AGENT_COMMS_NATIVE_ORIGIN_POLICY";
    throw error;
  }
  const origin = selected.origin;
  function requireDirectTransport(init) {
    // Pi may apply proxy settings after bootstrap. Refuse them at each call;
    // do not silently send a permitted localhost URL through an external proxy.
    if (["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
        .some(name => process.env[name]) || init?.dispatcher !== undefined) {
      const error = new Error("Native origin policy requires the default transport without a proxy or caller dispatcher");
      error.code = "ERR_AGENT_COMMS_NATIVE_ORIGIN_TRANSPORT";
      throw error;
    }
  }
  requireDirectTransport();
  function guardFetch(transport) {
    if (typeof transport !== "function") {
      const error = new Error("Native origin policy requires a fetch transport");
      error.code = "ERR_AGENT_COMMS_NATIVE_ORIGIN_TRANSPORT";
      throw error;
    }
    return async function fetch(input, init) {
      requireDirectTransport(init);
      // Normalize once, using the Request implementation installed with fetch.
      // Passing that same request prevents a second URL coercion at dispatch.
      const request = new globalThis.Request(input, init);
      const requestedOrigin = new URL(request.url).origin;
      if (requestedOrigin !== origin) {
        const error = new Error(`Native fetch origin refused: ${requestedOrigin}`);
        error.code = "ERR_AGENT_COMMS_NATIVE_ORIGIN_REFUSED";
        error.allowed_origin = origin;
        error.requested_origin = requestedOrigin;
        throw error;
      }
      // A permitted endpoint must not redirect into a different destination.
      // Refuse every redirect instead of allowing the transport to follow one.
      return Reflect.apply(transport, globalThis, [request, { redirect: "error" }]);
    };
  }
  let guardedFetch = guardFetch(globalThis.fetch);
  // Pi's configureHttpDispatcher calls undici.install(), which assigns fetch.
  // Keep that supported replacement behind the same decoded origin policy.
  Object.defineProperty(globalThis, "fetch", {
    enumerable: Object.getOwnPropertyDescriptor(globalThis, "fetch")?.enumerable ?? true,
    configurable: false,
    get() { return guardedFetch; },
    set(transport) {
      if (transport !== guardedFetch) guardedFetch = guardFetch(transport);
    },
  });
}

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
