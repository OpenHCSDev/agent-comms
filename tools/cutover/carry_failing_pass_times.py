"""Give each stored failing service pass the time of its newest failure.

FailingPass now records failed_at beside since, so the install's heartbeat probe can tell the
replaced runtime's failure from the new one's. A stored failure predates this carry, so its
newest failure is known only to be no earlier than its first: failed_at takes since, which is
older than any install that runs this step, so the probe never refuses on it.
Delete this step after the next installation.
"""

from typing import ClassVar

from pydantic import JsonValue

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.service_passes import FailingPass, ServicePasses, ServicePassStore
from agent_comms.store_files import AtomicFile


class FailingPassTimes(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (ServicePassStore,)

    def carry(self, *, apply: bool) -> Carried:
        failing_key = FieldCodec.record_key(ServicePasses, "failing")
        since_key = FieldCodec.record_key(FailingPass, "since")
        failed_at_key = FieldCodec.record_key(FailingPass, "failed_at")
        changed = 0
        rewritten = []
        for path in self.input_files():
            document = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(path.read_text()))
            failing = FieldCodec.decode(dict[str, dict[str, JsonValue]], document[failing_key])
            stale = {
                watch: failure for watch, failure in failing.items() if failed_at_key not in failure
            }
            if not stale:
                continue
            carried: dict[str, JsonValue] = {
                watch: failure | {failed_at_key: failure[since_key]} if watch in stale else failure
                for watch, failure in failing.items()
            }
            document[failing_key] = carried
            FieldCodec.decode_document(ServicePasses, document)
            changed += len(stale)
            if apply:
                AtomicFile(path).write_text(FieldCodec.compact(document, allow_nan=False))
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail={"failures": changed}, rewritten=tuple(rewritten))
