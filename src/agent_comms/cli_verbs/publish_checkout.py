from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_support.home_checkout import HomeCheckoutCliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, GitCommitText, PathText
from agent_comms.projects.checkout_publication import CheckoutPublication
from agent_comms.projects.storage_projection import StorageProjection


@dataclass(frozen=True)
class PublishedCheckoutResult(CliResult):
    published: Annotated[Path, PathText]
    files: int
    head: Annotated[str, GitCommitText]


@dataclass(frozen=True, kw_only=True)
class PublishCheckoutCliCommand(HomeCheckoutCliCommand, declared_name="publish-checkout"):
    help = "Copy a new verified bundle to ignored output and atomically replace its publication link"
    source: Annotated[Path, PathText, Argument(flags=("--source",))]
    destination: Annotated[Path, PathText, Argument(flags=("--destination",))]
    link: Annotated[Path, PathText, Argument(flags=("--link",))]
    previous: Annotated[Path, PathText, Argument(flags=("--previous",))]
    head: Annotated[str, GitCommitText, Argument(flags=("--head",))]

    def apply(self, ctx: Comms) -> PublishedCheckoutResult:
        thread, checkout = self.home(ctx)
        rule = ctx.registry.storage_rule
        source = self.source.expanduser().absolute()
        scratch = rule.roots_of(thread).scratch.resolve()
        if not source.is_relative_to(scratch):
            raise ValueError(f"Stage the validated publication bundle under {scratch} first")
        StorageProjection(rule, thread, ctx).require_write(source)
        for relative in (self.destination, self.link):
            rule.require_destination(thread, checkout.path / relative)
        rule.require_floor()
        publication = CheckoutPublication(checkout=checkout, source=source, destination=self.destination, link=self.link, previous=self.previous, head=self.head)
        destination, files = publication.publish()
        return PublishedCheckoutResult(destination, files, self.head)
