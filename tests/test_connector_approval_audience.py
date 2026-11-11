"""A browser the tailnet already admitted reads its own approval link, and every authorize
call issues the secret it minted."""

from __future__ import annotations

import time

import pytest

from agent_comms.transport.connector import (
    ApprovalAudience,
    PublicAudience,
    TailnetAudience,
)
from agent_comms.transport.connector_store import (
    AuthorizationQuery,
    ClaudeCallback,
    ConnectorGrants,
    digest,
)

LINK = "https://laptop.example.ts.net/approve/some-request/some-secret"


class Arrived:
    """Only the headers decide the audience, so a test states them and nothing else."""

    def __init__(self, **headers: str) -> None:
        self.headers = headers


def query() -> AuthorizationQuery:
    return AuthorizationQuery(
        response_type="code",
        client_id="client",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        code_challenge="challenge",
        code_challenge_method="S256",
    )


def test_tailscale_marks_the_public_arrivals() -> None:
    """Tailscale sets this header on the way in from the internet and on nothing else, so a
    missing header is a caller tailscale admitted first (transport/served_endpoints.py)."""
    assert isinstance(ApprovalAudience.of(Arrived()), TailnetAudience)
    assert isinstance(
        ApprovalAudience.of(Arrived(**{ApprovalAudience.funnel_header: "?1"})), PublicAudience
    )


def test_a_tailnet_browser_is_shown_its_own_link() -> None:
    """The person at that browser is the owner; sending them elsewhere for their own link
    protects nobody and strands them whenever comms cannot deliver."""
    page = TailnetAudience().deliver(None, "claude.ai", LINK)
    assert LINK in page


def test_a_public_browser_is_never_shown_the_link() -> None:
    """Anyone who knows the hostname reaches /authorize through Funnel, so printing the link
    would let a stranger approve their own browser."""
    sent: list[tuple[str, str]] = []

    class Connector:
        def request_approval(self, client: str, link: str) -> None:
            sent.append((client, link))

    page = PublicAudience().deliver(Connector(), "claude.ai", LINK)
    assert LINK not in page
    assert sent == [("claude.ai", LINK)]


def test_every_authorize_issues_the_secret_it_minted() -> None:
    """A second call used to keep the first record and drop the secret it had just minted, so
    the browser held one this store had never kept and require_approval could only refuse it:
    one fact, the live secret, split between the caller and the store."""
    asked, now = query(), time.time()
    first = ConnectorGrants().waiting(asked, digest("first"), now)
    second = first.waiting(asked, digest("second"), now)

    assert second.require_approval(asked.request_id, "second", now).approval_digest == digest(
        "second"
    )
    with pytest.raises(Exception):
        second.require_approval(asked.request_id, "first", now)


def test_re_issuing_one_request_does_not_consume_the_queue() -> None:
    """Replacing a waiting request keeps one entry, so a retrying browser cannot fill the
    owner's approval queue by itself."""
    asked, now = query(), time.time()
    grants = ConnectorGrants()
    for attempt in range(ConnectorGrants.waiting_limit + 3):
        grants = grants.waiting(asked, digest(f"secret-{attempt}"), now)
    assert len(grants.requests) == 1
