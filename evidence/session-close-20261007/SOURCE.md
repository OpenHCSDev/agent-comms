# Scoped ACP attachment close

The official SDK request is `session/close` with `sessionId`. The response is
the official `CloseSessionResponse`, serialized to `{}` by the SDK router.
`CommsAgent.close_session` delegates to the existing session lifecycle.

Only `AttachedSessionLifecycle` implements and advertises close. The launch
fact `CommsAgent.use_unstable_protocol` supplies both capability admission and
the SDK `run_agent` router option; the normal `CommsClient` entrypoint enables
it. A directly constructed client defaults to stable protocol. An owning
`CommsAgent` does not advertise close, even with unstable routing enabled.
Its executing turn, input drain, annotations and owner release retain their
existing whole-owner shutdown contract.

An attached session is its original `RuntimeProxy`, keyed by SDK session ID.
It has no local turn, input drain, cursor publication or settings-result
custody: those operations forward to the executing owner. Close runs under
the same attachment lock as load/rebind and removes the proxy only after
joined retirement, including through cancellation of the close caller.
The proxy revokes its controller token, closes the subscription, joins its
reader and pending permission callbacks, and cancels/joins its outstanding
request sockets. Reconnection also joins old permission callbacks before
acquiring a new controller. No owner cancel/stop command is sent. A request
already dispatched may still complete at the owner; closing its connection
does not classify or replay that input.

Other session IDs, transports and observers remain independent. Closing an
unknown/already closed ID returns invalid parameters. Reload can acquire a
new original subscription. One SDK session ID is one attachment in a client;
this does not introduce independent duplicate views under the same ID or
allow one process to acquire different root/launch environments.

Source consumers read: SessionLifecycle/AttachedSessionLifecycle,
CommsAgent/CommsClient, RuntimeConnection/RuntimeProxy/RuntimeServer,
AcpRequestConsumer, TurnRunner, InputDrain, ConfigOptions/PendingRequests,
CursorPublication, AnnotationWorker, RuntimeRequest declarations and load
admissions. Existing prompt/settings/queue/cancel/compaction consumers all
use the same proxy request owner. Source/tests/tools AST parse had no
omissions; dynamic third-party lifecycle consumers were not exercised.

Two private source checks passed in 0.83 seconds using actual declaration,
process identity, storage, runtime sockets, controller permissions and the
official SDK router. They verify two sessions, another observer of the
closed thread, continued updates, denied abandoned permission, unchanged
live owner, reopening, an unsent request and capability refusal for owned
or stable-only agents. No native/model/provider process was started.
Earlier test preparation stopped on missing xdist options and an absent
scratch parent. The first entered batch passed the unsent-request check but
stopped the subscription check on a fixture assertion expecting the SDK
model rather than its serialized response; that assertion was corrected
from the actual `normalize_result` declaration. Originals remain in tool
history and private scratch; no installed acceptance is claimed.

Private source scratch:
`/home/ts/.cache/agent-scratch/mendel-session-close-20261007`.
Parent owns Toad transport/navigation and the future installed shared
transport verification. No build, installation or public owner change.
