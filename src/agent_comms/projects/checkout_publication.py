"""Controlled updates of a launch checkout, never ordinary source-edit authority."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from agent_comms.installer.harness_install import replace_link
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.storage.store_files import StoreGuard


class PublishingCheckout(Checkout, StoreGuard):
    """Git owns source updates; a publication only creates ignored output and a link."""

    def qualified_for(self, source: Checkout) -> PublishingCheckout:
        """The explicit physical target must publish the caller's selected repository."""
        path = self.path.expanduser().absolute()
        checkout = self.checked(path)
        if path != checkout.path:
            raise ValueError("Publication checkout must be a physical path")
        if Repo.from_origin(checkout.path).github != Repo.from_origin(source.resolved().path).github:
            raise ValueError("Publication checkout must match the selected project's repository")
        return checkout

    def head(self) -> str:
        return Repo.git(self.path, "rev-parse", "HEAD").strip()

    def default_ref(self) -> str:
        branch = Repo.from_origin(self.path).default_branch()
        if Repo.git(self.path, "symbolic-ref", "--short", "HEAD").strip() != branch:
            raise ValueError("Canonical publication requires origin's default branch")
        return f"refs/remotes/origin/{branch}"

    def require_head(self, expected: str) -> None:
        if self.head() != expected or Repo.git(self.path, "rev-parse", self.default_ref()).strip() != expected:
            raise ValueError("The publication checkout changed; revalidate the bundle first")

    def sync(self) -> str:
        """Fast-forward the default branch, preserving every local edit and untracked file."""
        with self.write_guard(self.common_dir / "publication"):
            reference = self.default_ref()
            if Repo.git(self.path, "status", "--porcelain", "--untracked-files=no").strip():
                raise ValueError("Canonical sync refuses local tracked changes")
            with Repo.acquiring():
                Repo.git(self.path, "fetch", "origin")
            Repo.git(self.path, "merge-base", "--is-ancestor", "HEAD", reference)
            Repo.git(self.path, "-c", "submodule.recurse=false", "merge", "--ff-only",
                     "--no-edit", reference)
            return self.head()

    def output(self, relative: Path) -> Path:
        """A physical, ignored, untracked output location, not a source-edit boundary."""
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("Publication paths must be nonempty checkout-relative paths")
        path = self.path / relative
        if path.parent.resolve() != path.parent:
            raise ValueError(f"Publication parent follows a symlink: {path}")
        if Repo.git(self.path, "--literal-pathspecs", "ls-files", "-z", "--", str(relative)):
            raise ValueError(f"Publication cannot replace tracked source: {relative}")
        if not Repo.answers(self.path, "check-ignore", "--quiet", "--no-index", "--", str(relative)):
            raise ValueError(f"Publication output must be ignored by Git: {relative}")
        return path


@dataclass(frozen=True)
class PublicationFile:
    """The source bytes and mode that must survive copying before a link can move."""

    relative: Path
    sha256: str
    mode: int

    @classmethod
    def capture(cls, root: Path) -> tuple[PublicationFile, ...]:
        found = []
        for path in sorted(root.rglob("*")):
            observed = path.lstat()
            if stat.S_ISREG(observed.st_mode):
                with path.open("rb") as stream:
                    found.append(cls(path.relative_to(root),
                                     hashlib.file_digest(stream, "sha256").hexdigest(),
                                     stat.S_IMODE(observed.st_mode)))
            elif not stat.S_ISDIR(observed.st_mode):
                raise ValueError(f"Publication requires regular files/directories: {path}")
        if not found:
            raise ValueError("Publication bundle is empty")
        return tuple(found)


@dataclass(frozen=True)
class CheckoutPublication:
    """An immutable new bundle and compare-before-replace publication link."""

    checkout: PublishingCheckout
    source: Path
    destination: Path
    link: Path
    previous: Path
    head: str

    def publish(self) -> tuple[Path, int]:
        with self.checkout.write_guard(self.checkout.common_dir / "publication"):
            self.checkout.require_head(self.head)
            destination = self.checkout.output(self.destination)
            link = self.checkout.output(self.link)
            previous = self.checkout.output(self.previous)
            if (destination == previous or destination in previous.parents
                    or previous in destination.parents):
                raise ValueError("New and previous publication bundles must be disjoint")
            for bundle in (destination, previous):
                if bundle == link or bundle in link.parents or link in bundle.parents:
                    raise ValueError("Publication bundles and link must be separate paths")
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"Publication destination already exists: {destination}")
            if not link.is_symlink() or link.resolve(strict=True) != previous:
                raise ValueError("Publication link changed; revalidate the previous bundle first")
            if not self.source.is_dir() or self.source != self.source.resolve():
                raise ValueError("Publication source must be a physical directory")
            before = PublicationFile.capture(self.source)
            # copytree exclusively creates the destination. A failed copy is retained and
            # never made current; another call cannot silently overwrite its evidence.
            shutil.copytree(self.source, destination)
            if before != PublicationFile.capture(destination) or before != PublicationFile.capture(self.source):
                raise ValueError("Publication bytes or modes changed during copying; link unchanged")
            self.checkout.require_head(self.head)
            if link.resolve(strict=True) != previous:
                raise ValueError("Publication link changed during copying; new bundle retained")
            replace_link(link, Path(os.path.relpath(destination, link.parent)))
            return destination, len(before)
