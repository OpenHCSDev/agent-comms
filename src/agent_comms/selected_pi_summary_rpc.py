"""Default-off selected-summary RPC seam for an operation-dedicated child.

This deliberately does NOT derive model/auth/extension parity or authorize a paid
request. Provider-free fakes exercise the exact child lifecycle; a future
native-selected route must supply separately reviewed attestation first.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .selected_pi_child_deadline import SelectedChildUnknown, arm_selected_child


@dataclass
class SelectedSummarySlot:
    owner: str
    session: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _attempt: asyncio.Task[dict[str, Any]] | None = field(default=None, init=False)

    async def run_selected_summary(self, *_: Any, **__: Any) -> dict[str, Any]:
        """No operational opt-in before Pi model/auth/extension parity review."""
        raise SelectedChildUnknown("Selected paid RPC remains disabled pending Pi parity")

    async def exchange_fake_rpc(
        self,
        *,
        operation: str,
        command: tuple[str, ...],
        receipt: Path,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        """Provider-free single fake RPC; cancellation cannot free an active slot.

        No user input is accepted by this API. Operation ID is echoed verbatim,
        and the dedicated namespace is retired/joined even on malformed output.
        This is not a production model-attestation grant.
        """
        if not 0 < timeout_seconds <= 5 or not operation or len(operation) > 256:
            raise ValueError("Bounded fake selected operation required")
        if self._attempt is not None and not self._attempt.done():
            raise SelectedChildUnknown("Previous selected operation is still retiring")
        task = asyncio.create_task(self._run_fake(operation, command, receipt, timeout_seconds))
        self._attempt = task
        # A cancelled waiter must not cancel retirement or leak an unobserved
        # background exception. The retained task remains inspectable.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def _run_fake(
        self,
        operation: str,
        command: tuple[str, ...],
        receipt: Path,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        async with self.lock:
            identity = await arm_selected_child(
                owner=self.owner,
                session=self.session,
                operation=operation,
                command=command,
                deadline_ns=time.monotonic_ns() + int(timeout_seconds * 1e9),
                receipt=receipt,
            )
            try:
                raw = await identity.send(
                    (
                        json.dumps(
                            {
                                "type": "selected_fake",
                                "owner": self.owner,
                                "session": self.session,
                                "operation": operation,
                                "incarnation": identity.identity.token,
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode()
                )
                response = json.loads(raw)
                if (
                    type(response) is not dict
                    or set(response)
                    != {"type", "owner", "session", "operation", "incarnation", "ok"}
                    or response
                    != {
                        "type": "selected_fake_response",
                        "owner": self.owner,
                        "session": self.session,
                        "operation": operation,
                        "incarnation": identity.identity.token,
                        "ok": True,
                    }
                ):
                    raise SelectedChildUnknown("Unbound fake selected response")
                return response
            except (ValueError, UnicodeError) as error:
                raise SelectedChildUnknown("Malformed fake selected response") from error
            finally:
                # Task, not its waiting caller, owns the slot through join.
                await identity.retire()
