"""Explicit combined-carrier merge promotion and stack-contract verification."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.installer.runtime_resolution import StackSources
from agent_comms.planning.goal_management import Goals
from agent_comms.planning.orders import HeadReviewRecord, HeadVerdict
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.head_review import (
    AcceptedHeadReview,
    HeadReview,
    IntegrationInput,
    MergeArm,
    PullRequestHead,
    UnpromotedHeadReview,
)
from agent_comms.projects.repo import Repo
from agent_comms.projects.work_branches import WorkBranches
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms_hooks import ToolRefusedError


class MergeHeadReview(HeadReview, declared_name=None):
    """An explicit permission to merge, separate from accepting source for combination."""

    context = "agent-comms/merge"

    @classmethod
    def required_contexts(cls) -> tuple[str, ...]:
        return (*super().required_contexts(), cls.context)

    @classmethod
    def require_combined(
        cls, request: PullRequestHead, repositories: tuple[Repo, ...]
    ) -> IntegrationInput:
        repository = Repo.selected(repositories, request.repo.to_wire())
        captured = IntegrationInput.from_message(
            WorkBranches(repository.checkout).commit_message(request.head)
        )
        if not captured:
            raise ToolRefusedError(
                "Only a captured combined carrier may merge; accept individual source with judge ok"
            )
        (inputs,) = captured
        inputs.validate(repositories, request)
        StackSources.require_carrier_contract(
            repository,
            inputs.local.baseline.commit,
            request.head,
            {source.repository for source in inputs.sources},
        )
        inputs.require_source_reviews()
        return inputs

    @classmethod
    def require_merge(
        cls, request: PullRequestHead, repositories: tuple[Repo, ...]
    ) -> IntegrationInput:
        inputs = cls.require_combined(request, repositories)
        cls.require_permission(request)
        return inputs

    @classmethod
    def require_verdict_merge(
        cls, verdict: type[HeadVerdict] | HeadVerdict, request: PullRequestHead,
        repositories: tuple[Repo, ...],
    ) -> IntegrationInput:
        """The queue merges only a promoted combined carrier of ok sources."""
        verdict.require_ok(request)
        return cls.require_merge(request, repositories)

    @classmethod
    def require_permission(cls, request: PullRequestHead) -> None:
        if not (
            HeadReview.accepted(request.repo, request.head)
            and cls.accepted(request.repo, request.head)
        ):
            raise ToolRefusedError(
                "The exact combined carrier has no explicit reviewed merge promotion"
            )


@dataclass(frozen=True)
class PromotedHeadReview(MergeHeadReview, AcceptedHeadReview):
    """A forge permission, never a persisted source-review verdict."""

    def description(self, reviewer: str, subject: str) -> str:
        return f"Explicitly promoted by {reviewer} on {subject}"

    def require_sources(
        self, goals: Goals, request: PullRequestHead, reviewer: ThreadIncarnation
    ) -> None:
        snapshot = goals.registry.snapshot()
        project = snapshot.team_project.require_scope(snapshot, reviewer, request.repo)
        inputs = self.require_combined(request, project.repos)
        goals.todos.require_input_sources(inputs)

    def enqueue(self, record: HeadReviewRecord, repositories: tuple[Repo, ...]) -> MergeArm:
        request = record.request
        if not HeadReview.accepted(request.repo, request.head):
            raise ToolRefusedError("The exact combined source review must succeed before promotion")
        current = request.current()
        self.require_combined(request, repositories)
        self.post(request.repo, request.head, record.actor.name, f"#{request.pr}")
        try:
            self.require_merge(request, repositories)
        except ToolRefusedError, GitCommandError:
            UnpromotedHeadReview().post(
                request.repo, request.head, record.actor.name, f"#{request.pr}"
            )
            raise
        return current.arm()
