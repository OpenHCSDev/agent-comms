"""Client-side held UI profiles: lightweight import surface with no server/Core store deps."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.transport.connector_profile import (
    MCP_PATH,
    UiConnectionProfile,
    UiGrantId,
)


def connector_state_directory() -> Path:
    """Where this machine keeps its connector state, grants and held profiles alike."""
    return Path.home() / ".local/state/agent-comms/connector"


class HeldProfile(ABC):
    """One remote bus this machine holds a profile for, as a UI offers it.

    An unusable profile is a member too: a grant that expired or a file that will not
    decode is what the owner needs to see, not a row quietly missing from a menu.
    """

    path: Path

    def replacement_grants(self) -> tuple[UiGrantId, ...]:
        """Grants this entry replaces when another profile is accepted."""
        return ()

    @abstractmethod
    def label(self) -> str:
        """How a UI names this entry in one line."""


@dataclass(frozen=True)
class ReplaceableProfile(HeldProfile):
    """A held grant that a successful replacement revokes on its peer."""

    path: Path
    profile: UiConnectionProfile

    def replacement_grants(self) -> tuple[UiGrantId, ...]:
        return (self.profile.credential.grant_id,)


@dataclass(frozen=True)
class OpenableProfile(ReplaceableProfile):
    """A decoded profile whose grant has not expired."""

    def label(self) -> str:
        return f"{self.profile.endpoint.url.host} · {self.profile.credential.namespace}"


@dataclass(frozen=True)
class ExpiredProfile(ReplaceableProfile):
    """Decoded, but its grant's own expiry has passed; the server would refuse it."""

    def label(self) -> str:
        return f"{self.profile.endpoint.url.host} · grant expired"


@dataclass(frozen=True)
class MisdirectedProfile(HeldProfile):
    """Decoded and unexpired, and its endpoint is not where a connector serves MCP.

    A profile minted for the bare origin posts to / and is answered 404; held beside the
    good ones it was a row that looked identical and failed (Tristan, 2026-11-07: "i either
    get 404 or ..."). Shown rather than hidden, because the owner is the one who can delete
    it, and the reason is in the row.
    """

    path: Path
    profile: UiConnectionProfile

    def label(self) -> str:
        return (
            f"{self.profile.endpoint.url.host} · points at "
            f"{self.profile.endpoint.url.path or '/'}, not {MCP_PATH}"
        )


@dataclass(frozen=True)
class UnreadableProfile(HeldProfile):
    """A file in the held directory that is not a private profile this build can read."""

    path: Path
    refusal: str

    def label(self) -> str:
        return f"{self.path.name} · {self.refusal}"


class HeldUiProfiles:
    """The remote buses a UI on this machine can open.

    A UI grant lives in the connector store of the machine that serves its bus, keyed by
    the digest of the token this machine holds, so that store cannot answer what this
    machine can reach. These files are the connecting side's own answer, in one declared
    place, so a UI lists them instead of taking a profile path on its command line.
    """

    @classmethod
    def directory(cls) -> Path:
        return connector_state_directory() / "profiles"

    @classmethod
    def held(cls, now: float) -> tuple[HeldProfile, ...]:
        """Every profile file this machine holds, by name. No directory holds none."""
        directory = cls.directory()
        if not directory.is_dir():
            return ()
        return tuple(cls.entry(path, now) for path in sorted(directory.iterdir()))

    @classmethod
    def entry(cls, path: Path, now: float) -> HeldProfile:
        try:
            profile = UiConnectionProfile.read(path)
        except (OSError, ValueError) as refusal:
            return UnreadableProfile(path, str(refusal))
        if profile.credential.expires_at <= now:
            return ExpiredProfile(path, profile)
        if profile.endpoint.url.path != MCP_PATH:
            return MisdirectedProfile(path, profile)
        return OpenableProfile(path, profile)
