# Q5: declaration-owned active route boundary

Deleted50 old production lines across active_route.py and wire_metadata.py.
The eleven-part identity condition, manual record key/type/version shape and
handwritten output record are replaced by the existing FieldCodec operating on
the actual ActiveRoute declaration. Literal version1 remains required on the
wire through the existing wire_required capability, despite the constructor
default. The external four-field JSON representation is unchanged.

Root and package fields share AbsoluteRoutePathText, extending the existing
PathText field capability. WireRootIdText belongs to the private bus declaration;
both that record's construction and the route boundary use its one root-ID
spelling rule. No FieldCodec subclass, shape-union decoder, second route record,
cache, registry, compatibility method or authority mechanism.

Descriptor/owner/mode/inode/read-stability checks, directory locks, no-replace
publication, preflight, fsync UNKNOWN and explicit/environment route selection
remain. Decoded values are trusted after their boundary.

Focused SOURCE receipt:71 passed5.53s including actual route files/private
markers, required fields, malformed identities, rotation observation, publication
faults and the paired codec ownership guard. The first run had70 passed/one
failure: an old diagnostic expected a participant to be absent until worker
spawn. Unchanged current main reproduces that same failure; current delivery
correctly requires committed participant identity before the first message.
The assertion now checks that pre-spawn identity and retains the actual launch
and cohort acceptance checks. Both RED receipts are retained.

Parent still owns noneditable installed native/ACP/UI/default-route gate and
paired deployment. This draft does not claim SOURCE tests prove LIVE readiness.
CI is deferred. Whole refactor and absolute size acceptance remain active.
