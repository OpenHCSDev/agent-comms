# Native admission method annotations on supported Python

Python3.11 evaluates method annotations while constructing the class. Both
`NativeAdmissionEpoch.record` and `UnrecordedNativeAdmission.record` referenced
`NativeSessionIdentity`, imported only under `TYPE_CHECKING`; importing the
module therefore failed before the existing admission owner could run.

The module now uses the existing postponed-annotation mechanism, composing
both method declarations with the same import lifetime used by every other
native-session identity argument declaration in the production tree. The
original Package AST parsed316 modules with0 omissions:27 identity argument
sites, with exactly these two lacking postponed annotations. No new runtime
identity import, resolver, compatibility reader or import-cycle edge was added.

`NativeRuntimeInput` still owns the stored epoch field; `PrivateSendAdmission`
still invokes the epoch's exact SQL CAS before delegate input writing. Cursor,
reservation, broker and released-owner consumers retain their original behavior.
FieldCodec resolves record-class fields, not these method annotations. No record
field, admission fence, SQL, goal, session, native artifact or reader changed.

The parent and Einstein confirmed no overlapping writer. The W6/schema-carry
wheels and frozen controls are unchanged. This successor is independent of663
and its next explicitly granted target window.
