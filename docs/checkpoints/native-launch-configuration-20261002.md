# Native launch configuration ownership

Arendt owns this continuation from current main a8a3ba05. Native6 candidate
28401d70 and its configured journey remain Einstein’s independent frozen scope.

The existing RestartEnvironment will decode the original process configuration
at the OS boundary. NativePiRpcLaunch retains that contract: credentials/global
discovery and the writable Pi agent directory are distinct facts. A saved-session
fork destination never selects its owner’s credentials or extensions.

Migrate ordinary CLI, managed/tracked launch, selected settings invalidation,
persistent child auth invalidation and catalog discovery together. Remove repeated
configuration selection from those consumers. RetainedOwnerLaunch keeps its exact
process/configuration observation; environment encoding happens at process launch.
No native or durable format change, original input replay or public action.

Source ownership and caller migration precede one batched sanity check and the
affected installed configured journey. This document does not claim readiness.
