# W5: The harness classifies, never the agent

**Index:** [README.md](README.md).

## Target

- **A worker in agent-comms' runtime** listens for `ContextObserved`, finds spans with no annotation for the current classifier version, classifies them under W4's policy, stores the labels (W3) and publishes a typed `ContextAnnotated` event.
- **Never in the agent's loop:** classification runs beside the turn and never delays it. The agent being observed never labels its own working memory, which is why the existing pi extensions exposing Jev as an agent's tool are the wrong integration point.
- **Bounded:** a per-hour request budget declared in configuration; when it's exhausted, spans wait for the next window and show as unclassified.

## Done when

A turn's new spans are labelled within the budget without the turn waiting, and Toad receives `ContextAnnotated` for them.
