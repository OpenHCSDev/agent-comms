The shared incoming frame now shows the original destination and derives the reply destination from Message.exact_reply_target. Codex and OpenCode consume this frame. Claude Monitor already carries its target in its own reversible header and is unchanged.

Native patch applied one rewrite; no imports, routing rules, original bodies or message identities changed. Direct execution checked two channels (#comms and #nra) and a DM: each original body and ID survives, channels reply to themselves, and DM replies to sender. All three passed. No suite or profile.

Installed channel delivery remains to be checked after ordinary install.
