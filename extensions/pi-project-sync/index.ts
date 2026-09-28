/** Hold subsequent tools until a managed project handoff finishes. */
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const thread = process.env.PI_AGENT_ID || process.env.AGENT_COMMS_THREAD;
  if (!thread || process.env.AGENT_COMMS_MANAGED !== "1") return;
  pi.on("tool_call", (event, ctx) => {
    if (event.toolName === "comms_set_project") return;
    const state = JSON.parse(execFileSync("agent-comms", ["thread", "--name", thread, "--no-pending"], {
      encoding: "utf8", timeout: 30_000,
    }));
    if (resolve(state.worktree) !== resolve(ctx.cwd)) {
      return { block: true, reason: "Your project directory changed. End this turn now; the runtime will automatically continue in the new directory. Do not run more tools in the old project." };
    }
  });
}
