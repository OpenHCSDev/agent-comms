# Native SDK origin policy — source checkpoint

Owner: Original Sch. Qualifier owner: Original Mendel, PR682/Toad472.
Status: source only; no deployment, pin, wheel, import, or runtime qualification.

## Policy and selected request path

The existing `src/agent_comms/pi_project_bootstrap.mjs` reads
`AGENT_COMMS_NATIVE_ORIGIN` once, before its first dynamic SDK import. Absent
means the existing unrestricted configured transport is unchanged. Present
must be a canonical HTTP origin on numeric `127.0.0.1` or `[::1]`, without a
path, credentials, query, fragment, or alternate numeric spelling. The fixture
supplies `http://127.0.0.1:<actual bound port>`; models.json separately supplies
that origin plus `/v1` as the OpenRouter baseUrl.

The bootstrap owns the global fetch accessor. Its setter wraps Pi's supported
`undici.install()` replacement under the same origin policy; a one-time fetch
assignment would be overwritten during CLI setup. Each call normalizes one
Request, checks its origin, and passes that same Request to the transport.
Every redirect is refused with `redirect: "error"`. Proxy environment settings
(upper/lower HTTP_PROXY, HTTPS_PROXY, ALL_PROXY) and caller `init.dispatcher`
are refused, including settings applied after bootstrap. Nothing silently
clears or substitutes a configured proxy.

The selected immutable7a source establishes this path:

1. Pi CLI/setup/main uses `dist/core/http-dispatcher.js::configureHttpDispatcher`.
   It sets the EnvHttpProxyAgent and calls `undici.install()`, which assigns
   global fetch and Request. Both initial setup and later reconfiguration keep
   fetch behind the bootstrap accessor.
2. `@earendil-works/pi-ai/dist/api/openai-completions.js::createClient` constructs
   OpenAI with model.baseUrl and `options?.fetch`. The qualifier supplies no
   custom fetch or dispatcher.
3. OpenAI 6.40.0 `client.mjs` selects
   `options.fetch ?? Shims.getDefaultFetch()`. `internal/shims.mjs` returns global
   fetch. `fetchWithTimeout` invokes that captured function with URL and options.
4. Pi invokes `client.chat.completions.create(payload, requestOptions)` with
   maxRetries zero. Its higher provider retry policy is independently configured
   by the qualifier; it must not be used to infer dispatch absence.

No custom extension or NODE_OPTIONS code is introduced. Original launchers
continue stripping NODE_OPTIONS/NODE_PATH/NODE_COMPILE_CACHE and preloading the
committed import fence and project bootstrap. The data selector travels through
the existing launch environment. The normal cwd/session bootstrap remains.

## Error contract for the original qualifier

* Invalid selector: `ERR_AGENT_COMMS_NATIVE_ORIGIN_POLICY`, before SDK import.
* Proxy, caller dispatcher, or invalid replacement transport:
  `ERR_AGENT_COMMS_NATIVE_ORIGIN_TRANSPORT`.
* Different request origin: `ERR_AGENT_COMMS_NATIVE_ORIGIN_REFUSED`, with
  `allowed_origin` and `requested_origin`. It is thrown before the underlying
  fetch is invoked. The message includes only the refused origin, not URL paths,
  headers, credentials, or body.
* A redirect produces the transport's own fetch rejection; do not mislabel it
  as the explicit origin refusal code.

OpenAI `makeRequest` preserves the refusal as APIConnectionError.cause.
Pi's stream error normalization subsequently drops the cause and retains only
the generic connection message. Consequently a generic CLI failure is **not**
proof that the origin guard was reached.

For the reached-refusal control, use the selected committed package's
`node_modules/openai/client.mjs` **named `OpenAI` export** through the existing
authored Node helper/import fence/bootstrap launch. First use the real Pi HTTP
dispatcher setup so its fetch replacement is exercised. Construct the client
with the deliberately external baseURL, dummy apiKey, maxRetries: 0, and **no**
fetch/fetchOptions dispatcher override. Invoke the original
`client.chat.completions.create` API. Preserve the caught error's cause chain;
require the exact refusal code and both origins. Never accept a generic
connection error, DNS failure, unused guard declaration, or direct fetch-only
negative as the SDK refusal qualification. This adds no extension entry.

Mendel owns the exact authored control and any dispatch witness. Future issued
acceptance must bind its literal source/argv/output and prove the refused SDK
request did not reach external dispatch. Sch has not executed this control.
`configureHttpDispatcher(timeoutMs = DEFAULT_HTTP_IDLE_TIMEOUT_MS)` exports from
`dist/core/http-dispatcher.js`; the default constant is `300_000`. Call it without
arguments. It constructs a fresh EnvHttpProxyAgent and sets the global dispatcher
on every call. Its first install condition is equality with the global fetch
captured at module import; subsequent installs require equality with the
recorded installedGlobalFetch. The bootstrap accessor returns a stable wrapped
function until that assignment, so a repeated zero-argument call follows the
supported `undici.install()` path again. This is source-derived, not a runtime
receipt. Observe `undici:request:create` through diagnostics_channel without
replacing fetch/dispatcher; a refused-origin request must produce zero events
for that origin, while localhost positive requests demonstrate the observer
actually sees dispatch. A localhost redirect GET endpoint can attest that
redirect:error does not follow its Location. No runtime control ran here.

The original four native/MCP cases remain the localhost positive acceptance.
Also retain actual redirect refusal and dispatcher-reconfiguration evidence;
source inspection alone is not those outcomes.

## Honest boundary

This guards the selected SDK default fetch path under trusted committed Pi/
Undici code. It is **not** process-wide network isolation. Direct node:http/net,
direct imported undici.fetch, SDK options.fetch, arbitrary custom global
dispatchers/transports, WebSocket, MCP subprocesses, and other provider transports
are outside this claim. A malicious fetch replacement can ignore its arguments;
the setter supports the traced trusted Undici replacement, not hostile code.

If acceptance requires those other routes to be contained, the original Linux
launch owner (`NamespaceContainment.namespace_argv`) needs a separately designed
network namespace/egress policy that permits the actual localhost fixture.
The existing seccomp network denial blocks localhost too and cannot supply
this positive/negative contract unchanged.

## Deployment and resource relationships

Only the canonical bootstrap source changes. `stack/bin/prepare-pi-native`
already copies it to `dist/agent-comms-project-bootstrap.mjs` in a newly staged
deployment; `stack/native-import-manifest.json` and loader approvals are unchanged.
Full-tree verification covers the deployed bootstrap. The shared immutable7a
package/tree and `stack/pi-native.sha256` are unchanged and do **not** contain
this guard. No current7a executable tuple is admitted by this source checkpoint.

A future separately authorized deployment must commit the changed bootstrap,
produce a new full native tree/pin and truthful consuming Core source/wheel
(bootstrap is a normal wheel member; native pin is a forced wheel resource).
Retained Core3fe is not source-equal to that future changed bootstrap/pin.
Deployment digest/path, wheel, holder, READ/EXEC authority, installed proof,
and qualification receipts remain unbound. Existing typed zero-input use of
retained3fe/b2e/Text67 and accepted functional460 delivery are independent.
