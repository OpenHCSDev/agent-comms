# Persistent native243/244 deployment prepared

2026-09-28. Ready for the parent's quiet activation; **not activated**.

- Deployment: `/home/ts/.local/share/agent-comms/native-entry-store-0d7ebb4f4b5aa1ec`
- Canonical package: `/home/ts/.local/share/agent-comms/native-entry-store-0d7ebb4f4b5aa1ec/node_modules/@earendil-works/pi-coding-agent`
- Allocated: **203,149,312 bytes (193.74 MiB)**, including retained manifest.
- Source: Darwin's complete `stack/.pi-native-0d7ebb4f4b5aa1ec` bundle in
  `/home/ts/wt/comms-native-session-entry-store-20260928`.
- Destination absence checked before exclusive creation. Complete npm layout
  copied without links; all write bits removed, including directory write bits.
  This is immutable by deployment policy, not a same-UID security boundary.
- Copied manifest matches current receiver's `stack/pi-native.sha256`:
  `0d7ebb4f4b5aa1ec9df6c8b3ebdae32fc00673929952e990edc603bd1af4c799`.
  Full native package verification passes before and after the CLI checks.

## Actual acceptance

Fresh native CLI and copied synthetic saved-session CLI both pass from the new
canonical package. Each loads the deployed import fence and project bootstrap,
answers two stable selected-session identity handshakes, advertises
`pi-native-input-v1-live-only`, and returns the expected zero/two messages.
No extensions, tools, prompts or provider requests; test-only network denial
preload remains outside the deployment and is removed with the fixtures.
Both exact child identities are reaped; stderr is empty on both successful cases.

The source transcript and copied history prefix remain byte-identical. Native
startup appends one ordinary `thinking_level_change` to the copy. Initial probe
iterations corrected fixture setup: declare model context explicitly, use
owner-only transcript permissions, and allow this legitimate metadata append.
No production/package changes were needed.

Raw results: `deployment-receipt.json`. Reproducible probe:

```sh
PYTHONPATH=src /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python \
  evidence/native-entry-store/deployment_handshake.py \
  /home/ts/.local/share/agent-comms/native-entry-store-0d7ebb4f4b5aa1ec \
  .artifacts/s13-native-deployment
```

Owned disposable fixtures are removed automatically; empty artifact directories
were removed after acceptance. The persistent bundle intentionally remains.
No launcher, active route, existing owner, installed wheel, or user session was
modified. No large-capacity run was repeated. Parent owns pairing/activation.
