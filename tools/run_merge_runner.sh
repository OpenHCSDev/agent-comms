#!/usr/bin/env bash
# A repo runner is not a login shell. Hide host homes, buses, auth and sockets.
set -euo pipefail
state=${1:?usage: run_merge_runner.sh STATE_ROOT}
mkdir -p "$state/actions-runner/_work" "$state/actions-runner/_diag"
home=${HOME:?host home required}
user=$(id -un)
exec env -i \
  HOME="$home" USER="$user" PATH=/usr/bin:/bin LANG=C.UTF-8 \
  DOTNET_CLI_HOME=/tmp DOTNET_GCHeapHardLimit=0x10000000 \
  AGENT_COMMS_GATE_KEY="$home/.ssh/merge-gate-ed25519" \
  AGENT_COMMS_GATE_ENDPOINT="$user@127.0.0.1" \
  bwrap --unshare-all --share-net --die-with-parent --new-session --cap-drop ALL \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /etc/passwd /etc/passwd --ro-bind /etc/group /etc/group \
  --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
  --ro-bind /etc/resolv.conf /etc/resolv.conf --ro-bind /etc/hosts /etc/hosts \
  --ro-bind /etc/ssl/certs /etc/ssl/certs \
  --ro-bind /etc/ca-certificates /etc/ca-certificates \
  --dev /dev --proc /proc --tmpfs /tmp \
  --ro-bind "$state/runner-home" "$home" \
  --ro-bind "$state/actions-runner" /runner \
  --bind "$state/actions-runner/_work" /runner/_work \
  --bind "$state/actions-runner/_diag" /runner/_diag \
  --chdir /runner /runner/bin/Runner.Listener run
