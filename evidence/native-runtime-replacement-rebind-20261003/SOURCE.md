# One session replacement hook

Mendel owns this existing native-session-storage.patch/RPC consumer deletion;
Sch owns eventual immutable preparation/pins. #581 is independently Ready on
unchanged51b; this source followthrough does not edit that artifact or frozen385.

AgentSessionRuntime.finishSessionReplacement invokes its existing host callback
once after successful switch/new/fork/session conversion. RPC installs rebindSession
as that callback. The four RPC handlers also call rebindSession after the runtime
returns. That repeats bindExtensions, session_start, resources_discover and
subscriptions; extensions can write saved source. Delete all12 consumer lines.
Keep runtime hook, initial startup bind, correlation/response/cancel result and
all replacement checks. No new class/catalog/registry or boolean proof flag.

The whole268-module native AST from581 has no omissions, and the exact actual
runtime/RPC/InteractiveMode consumer implementations were read. InteractiveMode
already takes replacement behavior from the same runtime hook and only performs
its distinct UI/status/editor work after return. CLI and direct extension actions
also call runtime operations rather than repeating a second bind. The HTML-export
embedded bundle is browser source text, not this node runtime's lifecycle owner;
AST string literals are not dynamic or nested-JavaScript coverage proof.

Projection check: compare actual immutable51b RPC bytes with12 exact lines removed;
reverse original storage RPC patch, apply changed storage RPC patch with zero fuzz,
and verify byte equality plus node syntax. Before artifact unchanged. This is
source replay, not installed application or normal complete stock recipe proof.
A preliminary original native-input-only stock projection failed its unchanged
get_state trailing blank-line context; preserve raw rejection and let the builder
qualify its original stock recipe, not silently bypass the hash/zero-fuzz boundary.

Next: Sch captures the original normal recipe after reviewed source freeze and
establishes full native trust; use that artifact to verify actual no-provider SDK
replacement fires session_start once and cancellation preserves old runtime.
No repeat unchanged summary/provider/three-fork gate. Original581 same-child
configured receipt remains protected. No latency attribution beyond concrete
repeated source work, no unchanged public/default package mutation.
