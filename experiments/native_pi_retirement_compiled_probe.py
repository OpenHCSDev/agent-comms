"""Quarantined historical pinned Pi rootless-namespace probe (source: commit 7179cd7).

Its provider-free get_state failed before RPC due to the compiled Pi's private
ancestor check; its old numeric PID -> pidfd handoff was not identity-safe.
Do not rerun it. A separate, no-userns disposable lane is required instead.
"""

if __name__ == "__main__":
    raise SystemExit("Historical compiled Pi namespace probe is quarantined; no child was launched")
