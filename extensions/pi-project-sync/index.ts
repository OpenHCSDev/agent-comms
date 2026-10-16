/** Hold subsequent tools until the original managed project handoff finishes. */
import { once } from "node:events";
import { createConnection } from "node:net";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { CallerRecord } from "../pi-agent-comms/index.js";

async function project(socket: string, request: string) {
  const connection = createConnection({ path: socket });
  try {
    await once(connection, "connect");
    connection.write(request + "\n");
    const records = createInterface({ input: connection, crlfDelay: Infinity });
    try {
      for await (const line of records) {
        const reply = JSON.parse(line);
        if (reply.error) throw new Error(reply.error);
        if (typeof reply.result?.worktree !== "string") {
          throw new Error("Original project observation is unavailable");
        }
        return reply.result.worktree;
      }
    } finally {
      records.close();
    }
    throw new Error("Original project owner disconnected before replying");
  } finally {
    connection.destroy();
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerFlag("agent-comms-project-socket", { description: "Held original project socket", type: "string" });
  pi.registerFlag("agent-comms-project-request", { description: "Original typed project observation", type: "string" });
  pi.on("tool_call", async (event, ctx) => {
    let caller: CallerRecord | undefined;
    // The comms extension owns this record. Read it at use, do not mirror it.
    pi.events.emit("agent-comms:caller", (original: CallerRecord) => { caller = original; });
    if (caller === undefined) throw new Error("The comms caller provider is unavailable");
    if (caller.kind !== "thread" || event.toolName === "comms_set_project") return;
    const socket = pi.getFlag("agent-comms-project-socket");
    const request = pi.getFlag("agent-comms-project-request");
    if (typeof socket !== "string" || typeof request !== "string") {
      throw new Error("Managed native launch lacks its original project binding");
    }
    if (resolve(await project(socket, request)) !== resolve(ctx.cwd)) {
      return { block: true, reason: "Your project directory changed. End this turn now; the runtime will automatically continue in the new directory. Do not run more tools in the old project." };
    }
  });
}
