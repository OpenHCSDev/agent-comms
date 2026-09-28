## S9 K1–K6: source closure integrated, acceptance fixes in progress

PR236 consumes A12 through62adf9a and S10 strict metadata429232b (with current
A13 coordinator/native tables). K4 local transport/launcher/watchdog deleted;
manual and authority execution use A12. K5 fresh/manual startup parsing uses
S10's one NativeEntry family. All three S9 guards pass. Dead correction_revision
attestation echo and its current registration/caller fields are deleted.

Current evidence:38authority/package/boundary;36actualnative authority;76journal,
returned-ACK and owner boundary cases (one ACP case deliberately deselected after
its shared caller failure was recorded). Earlier real saved96row evidence stands.

Two concrete integration repairs remain with their existing owners:
- A12/235 ThreadManagement.claim_thread still invokes removed Thread(pid=), so
  actual ACP new_session fails before compaction. Reported232/235.
- S10 startup decoder rejects existing native parent UUID IDs; actual stock Pi
  produces this metadata on the saved-session fixture. Eight real manual cases
  fail before loopback HTTP; reported234. Preserve strict fresh parent matching.

Two tests mocking deleted manual signal/platform helpers removed, not adapted.
No CI/provider/live changes; parent owns quiet runtime journal reset and install.
