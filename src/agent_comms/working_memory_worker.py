"""Runtime-owned annotation work borrows original observations after commit."""
from __future__ import annotations

import asyncio
import os
from functools import partial

from acp.schema import SessionInfoUpdate

from .acp_extension import ContextAnnotatedUpdate, encode_updates
from .coordinator import Coordination
from .working_memory_annotations import PreviouslyRequestedAnnotation, WorkingMemoryAnnotations
from .working_memory_disclosure import DisclosureState
from .working_memory_labels import QuestionVersion
from .working_memory_policy import AnnotationPolicy
from .working_memory_questions import KindQuestion
from .working_memory_requests import AnnotationBudgetExhausted, DisclosureRequest, FailedAnnotationOutcome


class AnnotationWorker:
    def __init__(self, comms, runtime, effects, policy: AnnotationPolicy):
        self.comms, self.runtime, self.effects, self.policy = comms, runtime, effects, policy
        self.tasks: set[asyncio.Task] = set()

    def observe(self, session_id, manifest, values):
        self.policy.observe(self, session_id, manifest, values)

    def start(self, policy, session_id, manifest, values):
        sessions = self.runtime.agent.sessions
        publish = partial(self.runtime.session_update, session_id=session_id,
            _expected_client=sessions.client, _expected_thread=sessions.require(session_id),
            _expected_sockets=frozenset(self.runtime.clients.get(session_id, ())))
        task = self.runtime.background(self.run(policy, publish, manifest, values))
        self.tasks.add(task)
        task.add_done_callback(self.retired)

    def retired(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.effects._debug_log(f"annotation worker stopped: {type(error).__name__}")

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def store(self, operation):
        return await Coordination.run_async(self.comms.root / "coordination.sqlite3",
                                            lambda store: operation(store.annotations))

    async def answer(self, policy, manifest, segment, span, question):
        grant, rules = await Coordination.run_worker(
            partial(policy.acquire, self.comms, manifest.thread))
        grant.require_segment(segment)
        version = QuestionVersion.current(question, rules)
        rows = await Coordination.run_worker(partial(WorkingMemoryAnnotations.labels,
            self.comms.root / "coordination.sqlite3", span, version, grant.classifier))
        if rows:
            from .coordination_tables.annotations import SpanAnnotationsRow

            return SpanAnnotationsRow.effective(rows)
        state = DisclosureState.capture(segment, span, question, rules)
        request = DisclosureRequest.capture(span, grant.classifier, question, state, policy.source)
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise ValueError("Approved annotation route has no configured credential")
        # Reservation is durable before the first external byte. Cancellation
        # or process loss leaves Submitted intact; no subsequent run resends it.
        original = await self.store(lambda owner: owner.reserve(request, grant))
        try:
            response, label = await grant.classifier.classifier.classify(request, api_key)
        except Exception as error:
            await self.store(lambda owner: owner.fail(
                original, FailedAnnotationOutcome(type(error).__name__)))
            raise
        await self.store(lambda owner: owner.complete(original, response, label))
        return label

    async def run(self, policy, publish, manifest, values):
        labels = []
        for segment in values:
            for span in segment.public_spans():
                try:
                    label = await self.answer(policy, manifest, segment, span, KindQuestion)
                    labels.append(label)
                    for question in label.answer.follow_ups:
                        labels.append(await self.answer(policy, manifest, segment, span, question))
                except AnnotationBudgetExhausted:
                    break
                except (PreviouslyRequestedAnnotation, ValueError):
                    # Withheld or unsettled requests have no inferred label.
                    continue
        if labels:
            await publish(update=SessionInfoUpdate(session_update="session_info_update",
                field_meta=encode_updates(ContextAnnotatedUpdate(manifest, tuple(labels)))))
