Claude's existing header and constants now live on IncomingFrame. ClaudeMonitorFrame inherits them through MonitorIncomingFrame; its duplicate inline concatenation was deleted and its callers read the inherited text. Codex and OpenCode consume that same text. The shared header retains the source target, sender, exact reply target and original message reference.

Native transaction applied both files. Direct execution proved identical inherited methods for channel #comms, channel #nra and a DM, preserved original bodies/references, correct reply targets and unchanged Monitor header decoding. No suite or profile.

Installed channel delivery remains to be checked after ordinary install.
