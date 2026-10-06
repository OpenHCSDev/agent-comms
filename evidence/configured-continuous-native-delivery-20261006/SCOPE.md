# Configured continuous native delivery

The existing configured saved producer binds source529 and peer529 to its own RuntimeServer. Binding starts the server; it does not start the inbox observation tasks. The producer previously forced auto_wake=False. A continuous channel/DM journey therefore needs the existing input drain capability.

The sole changed method accepts auto_wake=False and forwards it to CommsAgent. When selected, it starts each original bound session through InputDrain.ensure_live_drain. Existing compaction/S4 callers keep their default. Toad opts in through the shared producer, with no copied declaration, participant injection or external RuntimeServer.

Original installed source verification, RetainedOwnerLaunch environment/model/thinking, SDK private fork, request worktree, restore_stopped admission, source/input-proof hashes and joined shutdown remain the same owner paths. Source compilation performed without application imports. Runtime, provider, SDK, mounted App and continuous acceptance remain unrun.
