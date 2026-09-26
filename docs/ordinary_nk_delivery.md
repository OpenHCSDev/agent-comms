# Ordinary sends into private N/K (incremental implementation)

On an **explicitly initialized private root**, the ordinary `comms_send` tool
now reaches the existing full-N/K publisher through `Comms.send_message` and
`MessageBus.publish_ordinary`. The source row contains its frozen audience and
trusted wake decisions in the same durable append. This is not a post-hoc
candidate row, an inferred audience, or a copy into an unrelated coordination root.

`publish_ordinary` never initializes a private marker or migrates an existing
legacy wire. It retains the Comms wire lock and dispatches under the bus lock.
The existing private publisher still checks its explicit writer enable flag,
private root/marker/registry guard, exact source revision and frozen audience.
Direct legacy `MessageBus.publish` remains blocked after private cutover.
Unmarked ordinary sends retain legacy behavior. Claim/release envelopes keep
their existing separate authority path; unrelated independent claims are not
reclassified or prohibited by this bridge.

The existing explicitly started foreground recipient consumes the committed
ordinary source, seals its N delivery/K selected receipts, and runs selected
triage or FULL through the binding/send boundary. All frozen participants must
already be committed to the same private coordination store. A send does not
silently enroll a recipient, start an executor, activate a provider, or infer
acceptance from an inbox ACK.

An opt-in `CommsAgent` ACP session can now consume that same selected private
source via its already registered process owner (`private_nk_wire_root_id` and
`private_nk_native_package` must both be passed to the constructor). Its live
drain verifies the private marker and reviewed native package, seals visible
committed initials using the same N/K coordinator, then runs at most one
selected native claim. It refuses to run alongside an ACP owner turn and never
uses legacy inbox cursors, dispositions, steer, or ACK on a marked private root.
An unconfigured ACP agent encountering a marked root fails closed instead of
falling back to legacy delivery. Public ACP roots still use their original
path. This is an explicit constructor-only pilot, **not** automatic enablement
by the production ACP entry point; it neither creates participants/schema nor
replays uncertain inputs. A bounded 100-initial scan remains a scale limit.

## Provider-free tests

`tests/test_ordinary_nk_delivery.py` invokes the ordinary `comms_send` tool, then
uses real bus/SQLite/foreground processing with only model responses faked:

- direct FULL, exact sender response route;
- channel mention FULL with a no-wake observer;
- unmentioned channel bounded triage → IGNORE;
- unmentioned channel bounded triage → FULL (two distinct inputs);
- addressed-to-other channel delivery with zero model calls;
- public legacy root unchanged; no marker/schema auto-install;
- direct legacy writer and explicitly disabled private writer still refused.

It checks exact N/K counts and historical binding equality, not native/provider
acceptance. Fake responses cannot close real protocol acceptance gates.

## Still incomplete

This is **not** completion of ordinary-delivery integration: a durable current
injection cursor, production ACP entry-point configuration and session-UI
receipt/reconnect coverage, mediated pre-write admission, append-driven refresh,
alias/human-path coverage, and scaling/deadline acceptance remain open. The
foreground consumer remains one-shot; no production ACP activation or legacy
historical input replay occurred. Shell/child and ordinary human ACP coding
writes remain unenforced by this selected-message path.
