/** Hold subsequent tools until a managed project handoff finishes. */
import { once } from "node:events";
import { createConnection } from "node:net";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

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
  const thread = process.env.PI_AGENT_ID || process.env.AGENT_COMMS_THREAD;
  if (!thread || process.env.AGENT_COMMS_MANAGED !== "1") return;
  const socket = process.env.AGENT_COMMS_PROJECT_SOCKET;
  const request = process.env.AGENT_COMMS_PROJECT_REQUEST;
  if (!socket || !request) throw new Error("Managed native launch lacks original project binding");
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName === "comms_set_project") return;
    if (resolve(await project(socket, request)) !== resolve(ctx.cwd)) {
      return { block: true, reason: "Your project directory changed. End this turn now; the runtime will automatically continue in the new directory. Do not run more tools in the old project." };
    }
  });
}
