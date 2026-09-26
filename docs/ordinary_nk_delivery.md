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
path. Independent review of the earlier ACP checkpoint `ace00a3` found a
second ACP instance could borrow its peer's active registry turn, stranding a
FULL claim or admitting two concurrent TRIAGE sends; it also found a goal
change after reservation could slip through final send. The successor claims
an exclusive canonical owner turn before engagement, refuses an unrelated
human/ACP turn, and pins the exact goal scheduling snapshot at turn CAS and
final raw-send admission. A stable preexisting goal is **not** globally banned
from an ordinary selected interruption. No early rejection implies a native
input retry. This correction needs its own exact independent review.
The production stdio ACP attachment and its separate owner worker now accept
an **explicit dormant configuration**: both
`AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=<32-hex-root>` and
`AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=<absolute-reviewed-copied-Pi-package>`
under the exact `AGENT_COMMS_ROOT`. Both entry points preflight before creating
or attaching their wire; the worker passes the pinned pair into its owner
`CommsAgent`. A marker alone cannot activate this path, half-configured or
wrong-root/package inputs fail closed, and `PI_PROMPT` cannot silently launch
an unbound legacy prompt in private mode. Nothing in this change sets those
variables on a live installation, creates participants/schema, or retries
uncertain inputs. An operator must separately perform fresh-root opt-in and
commit the frozen recipients. A bounded 100-initial scan remains a scale limit.

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

This is **not** completion of ordinary-delivery integration. An additional
explicit ACP owner candidate now publishes a *bounded* durable current-owner
cursor (canonical root/recipient/owner generation and admission epoch, exact
sealed source/claim/stage/native binding and journal request digest), and
exposes it in session updates/reconnect metadata. `covered_seq` can include
sealed no-wake/absent-audience sources; `injected_seq=0` means no native input.
A selected UNKNOWN source blocks prefix advancement; owner replacement yields
no current cursor, not historical recovery. A v3 native runtime input now
records the owner admission epoch under the sealed Pi send lock; cursor
advance/read checks that immutable input-to-epoch witness so a caller-supplied
old input ID cannot seed a new epoch. This cursor never authorizes an ACK,
skipped claim, response, write, provider acceptance or replay. The
100-initial/8 MiB historical scan still makes it unavailable at scale; existing
v1/v2 runtime schema roots require an explicit reviewed migration to v3 rather
than an implicit upgrade. In particular, historical v2 inputs cannot be assigned
a new send epoch from a journal. Independent exact review remains open.

Mediated pre-write admission, append-driven/indexed refresh, full alias/human
coverage and scaling/deadline acceptance remain open. The foreground consumer
remains one-shot; no production ACP environment was activated and no legacy
historical input replay occurred. Shell/child and ordinary human ACP coding
writes remain unenforced by this selected-message path.
