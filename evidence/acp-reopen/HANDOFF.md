# Toad ACP startup regression — 2026-09-28

Owner's ordinary Toad prompt to agent-comms-ux failed at native startup.
Diagnostic bfca4f2aec354546b1e0f7cd7f2c5b2f records native_preflight_exit,
exit1,3983ms, saved session143224441bytes. Both installed owner processes
remain alive. No failed prompt was retried by this investigation.

## Reproduction and actual cause

The original saved v3 session parses read-only into27979 entries. Its session
directory/files have the required700/600 permissions. The59MB proof journal
is below the native startup cap. Neither observation alone proves execution.

Parent copied only that session and journal into its own persistent
`.artifacts/acp-reopen` directory. With the installed pi-comms-native wrapper,
configured model, managed project and get_state only:

- Normal global discovery exits1 before get_state. agent-comms, project-sync,
  subagent and web-search extensions are rejected as outside the committed
  native deployment root. `project-preflight-stderr.txt` records the actual
  error. No prompt or provider call was sent.
- The same copied session with extensions disabled returns a successful native
  capability/get_state response. This is diagnosis, NOT the production fix:
  stripping configured extensions would remove existing functionality.
- First copied-fixture attempt had a755 directory and was correctly refused;
  fixture permissions were corrected to700. Its error is retained separately,
  not attributed to the original user session.

Normal ACP did not disable automatic global extension discovery, while earlier
controlled acceptance launches did. The native import boundary therefore
rejected a real user path that those acceptance launches did not exercise.

## Ownership and patch status

Darwin owns native launcher/package extension integration in
`~/wt/comms-native-global-extensions-20260928`. Preserve configured functionality
and deployment authority; extend existing packaging/manifest mechanisms.
Parent owns this tree's backend feedback patch, integration and live activation.
No installed package, global extension or original session was changed here.

The backend feedback patch preserves bounded startup stderr in the existing
InputIdUnavailable terminal result when preflight exits before sending a prompt.
Image-sensitive redaction remains; proof-journal-specific refusal still takes
precedence; persisted structural diagnostics do not acquire raw stderr.
Three focused backend checks plus seven journal/image boundary cases pass.
This improves error visibility but does not itself fix native extension loading.

## Required acceptance before activation

Package the complete native extension fix, prove normal automatic discovery with
the actual copied saved session and configured model without --no-extensions,
retain the native input capability and expected extension registrations, then
test the installed normal ACP/Toad path. Migrate the normal idle owners through
the supported lifecycle, preserving same bus/history and UNKNOWN inputs. Do not
replay the reported failed message. Remove the owned copied-session fixture after
acceptance; preserve compact receipts and the original user data.
