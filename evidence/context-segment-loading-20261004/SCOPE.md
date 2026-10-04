# Context contributor declaration loading

Current preview can return UnavailableAwarenessSegment from the server while an
ordinary client has never imported its producer module. FieldCodec resolves a
ContextSegment through its existing declaration registry, so the client rejects
that valid original contribution. Reader14 recorded text remains accepted;
its exact current-preview process timing is not inferred from this source cause.

The complete ContextSegment family includes native/current declarations and
retained, selected-wake/work/response and optional awareness declarations.
The root must acquire all declarations for decode and schema independently of
which producer happened to execute. Reuse metaclass-registry discovery and the
original leaf classes; delete producer-dependent registration paths. No frontend
imports, unknown-kind fallback, extra catalog or changed wire/native schema.

Source/AST and import lifetimes first. One final fresh-process codec/schema
batch prevents declaration-loading and cycle failures; no provider/UI rerun or
package loan. Existing 627 branch, original saved requests/UNKNOWN and installed
candidate remain untouched.
