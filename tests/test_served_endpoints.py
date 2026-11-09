"""What this machine serves, and which of it may hand a caller a UI profile.

The recorded reply is ts-gaming's own `tailscale serve status --json` on 2026-11-07: one
funnelled origin on 443 and one tailnet-only port, both in front of the same connector.
"""

from agent_comms.transport.served_endpoints import (
    FunnelServed,
    NothingTailnetOnly,
    OfferedByTailnet,
    ServedEndpoints,
    ServedTarget,
    TailnetServed,
)

TS_GAMING = {
    "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
    "Web": {
        "ts-gaming.tail553874.ts.net:443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:18766"}}},
        "ts-gaming.tail553874.ts.net:8443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:18766"}}},
    },
    "AllowFunnel": {"ts-gaming.tail553874.ts.net:443": True},
}


def test_the_funnelled_origin_and_the_tailnet_port_are_different_members():
    served = ServedEndpoints.listed(TS_GAMING)
    assert [type(endpoint).__name__ for endpoint in served] == ["FunnelServed", "TailnetServed"]
    funnel, tailnet = served
    # 443 is the bare origin; any other port belongs in the URL a profile is written for.
    assert funnel.target.url == "https://ts-gaming.tail553874.ts.net"
    assert tailnet.target.url == "https://ts-gaming.tail553874.ts.net:8443"
    assert (funnel.offers_profiles, tailnet.offers_profiles) == (False, True)
    assert "public (Funnel)" in funnel.label() and "tailnet only" in tailnet.label()


def test_the_endpoints_in_front_of_a_port_offer_the_tailnet_one_first():
    served = ServedEndpoints.listed(TS_GAMING)
    behind = ServedEndpoints.behind(served, 18766)
    assert [endpoint.target.url for endpoint in behind] == [
        "https://ts-gaming.tail553874.ts.net:8443",
        "https://ts-gaming.tail553874.ts.net",
    ], "a profile is offered for the tailnet-only endpoint before the public one"
    assert ServedEndpoints.behind(served, 18767) == ()


def test_a_machine_serving_nothing_offers_nothing():
    assert ServedEndpoints.listed({}) == ()
    assert ServedEndpoints.behind((), 18766) == ()


def test_a_served_path_keeps_its_path_and_a_root_handler_does_not():
    assert ServedTarget.at("host.ts.net:443", "/", "http://127.0.0.1:1").url == "https://host.ts.net"
    assert ServedTarget.at("host.ts.net:443", "/mcp", "http://127.0.0.1:1").url == (
        "https://host.ts.net/mcp")
    assert ServedTarget.at("host.ts.net:8443", "/", "http://127.0.0.1:1").url == (
        "https://host.ts.net:8443")


def test_a_target_recognises_the_port_behind_it():
    target = ServedTarget("https://host.ts.net:8443", "http://127.0.0.1:18766/")
    assert target.behind(18766) and not target.behind(1876) and not target.behind(18767)


def test_the_status_command_asks_tailscale_for_json():
    assert ServedEndpoints.status_command() == ("tailscale", "serve", "status", "--json")


def test_only_a_tailnet_only_endpoint_offers_a_profile():
    served = ServedEndpoints.listed(TS_GAMING)
    offered = ServedEndpoints.offer(served, 18766)
    assert isinstance(offered, OfferedByTailnet)
    assert offered.endpoint.target.url == "https://ts-gaming.tail553874.ts.net:8443"
    assert offered.refusal() == ""


def test_a_port_behind_a_funnel_alone_refuses_and_says_what_to_do():
    funnel_only = {
        "Web": {"host.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:18766"}}}},
        "AllowFunnel": {"host.ts.net:443": True},
    }
    refused = ServedEndpoints.offer(ServedEndpoints.listed(funnel_only), 18766)
    assert isinstance(refused, NothingTailnetOnly)
    refusal = refused.refusal()
    assert "No tailnet-only endpoint serves 127.0.0.1:18766" in refusal
    assert "public (Funnel)" in refusal, "the refusal names what is served instead"
    assert "tailscale serve" in refusal and "grant-ui" in refusal, "and both ways forward"


def test_a_port_nothing_serves_refuses_without_naming_endpoints():
    refused = ServedEndpoints.offer(ServedEndpoints.listed(TS_GAMING), 18767)
    assert isinstance(refused, NothingTailnetOnly)
    assert "serves nothing in front of it" in refused.refusal()


def test_both_probes_run_a_command_one_way():
    from agent_comms.transport.probed_command import ProbedCommand
    from agent_comms.transport.tailnet_buses import TailnetBuses
    assert issubclass(ServedEndpoints, ProbedCommand) and issubclass(TailnetBuses, ProbedCommand)
    # A classmethod binds per access, so the comparison is of the one function behind it.
    assert ServedEndpoints.run.__func__ is ProbedCommand.run.__func__
    assert TailnetBuses.run.__func__ is ProbedCommand.run.__func__
    # The one that crosses the tailnet declares its own bound; the local one takes the default.
    assert TailnetBuses.timeout_seconds > ServedEndpoints.timeout_seconds
