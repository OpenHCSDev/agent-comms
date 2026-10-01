# Declared family schemas

Parent owns this narrow FieldCodec contribution for S2 PR475. The existing
encoder/decoder already supports discriminator-selected dataclass families;
value_schema does not describe their instance values. This blocks the actual
DecisionScopeSelection tool catalog. Named tool payloads use record_schema:
their selector is already supplied by the API, outside the argument object.

Derive alternatives from the original DeclaredFamily membership and use its
declared discriminator for nested instance values. Preserve the named-payload
record_schema contract and share its existing field builder; preserve
all wire encoding, decoding, proofs and representations. No codec subclasses,
manual member roster, alternate decoder or payload format change.

Verify the codec's schema against its actual round trips and the receiving S2
tool catalog. Installed affected acceptance belongs to the paired S2 journey.

## Accepted shared boundary, original installed run34

Production3 lines deleted/21 added. Source38 and receiving catalog22 checks
passed; the original installed run34 now closes the native receiving boundary.
Parent compared the complete codec file byte-for-byte to installed Core3f0af494
and reviewed its original SDK catalog, native journal, InputDoc and wire message.
The SDK's33-tool catalog hashes to1ce0c73e/18049bytes, matching both native
provider-context manifests. Nested scope/change schemas are derived oneOf
families, with original/current discriminator cases successfully invoked by
native comms_decision. Its nonerror result and the canonical decision share
message6f69038c5f76/sequence1 and the original admitted turn identity.

Evidence: installed-native-boundary34.json and parent-installed-review.json.
No HTTP request JSON was retained before the unrelated helper assertion failed;
do not claim HTTP byte capture. That negative whole-run result is preserved.
This acceptance is the shared schema mechanism, not full S2/S5 feature or UI
readiness. No original input was replayed, paid call made or public default
changed. Full source owners continue their same combined journey.
