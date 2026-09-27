"""Provider-free pinned-RPC digest parity, without enabling a native send.

Constants come from the independently rerun frozen compiled _claimNativeInput VM
probe in /var/tmp/pr95-pr112-raw-first-independent-semantic-review-20260927.md.
That probe did not import Pi, open a pipe, or contact a provider. This test
never turns a pure digest into input reservation or writer authority.
"""

import pytest

from agent_comms.private_sidecar import (
    NativeRequestSource,
    encode_request,
    native_request_digest,
    native_request_digest_for_source,
)


@pytest.mark.parametrize(
    ("prompt", "compiled_rpc_digest", "historical_interactive_digest"),
    [
        (
            "bound prompt reply exactly",
            "7ffe3599d1348335c6431d9f15b8686365af31eed90a07dbbc6d7db4d1786911",
            "77bf28dd2ddcf8d7b85c6a545436db03a461208c1026dbcbc156177e771f436e",
        ),
        (
            'unicode café 日本語 "quoted"\nline two',
            "865c1e2eb1c9e637aca0c9b37155971154f05bf08329eeca7c8b82e2e8bb128f",
            "27730957b30b1befee4e44277df85e5d3801578d05de69244b24e01382b88482",
        ),
    ],
)
def test_typed_rpc_digest_matches_independent_compiled_control_without_rewriting_legacy(
    prompt: str, compiled_rpc_digest: str, historical_interactive_digest: str
) -> None:
    assert native_request_digest_for_source(prompt, NativeRequestSource.RPC) == compiled_rpc_digest
    assert native_request_digest(prompt) == historical_interactive_digest
    assert native_request_digest_for_source(prompt, NativeRequestSource.INTERACTIVE) == (
        historical_interactive_digest
    )
    assert compiled_rpc_digest != historical_interactive_digest
    assert '"source":"interactive"' in encode_request(prompt)


def test_source_lane_requires_nominal_enum_not_caller_selected_string() -> None:
    with pytest.raises(TypeError, match="typed source lane"):
        native_request_digest_for_source("hello", "rpc")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exact text"):
        native_request_digest_for_source(b"hello", NativeRequestSource.RPC)  # type: ignore[arg-type]
