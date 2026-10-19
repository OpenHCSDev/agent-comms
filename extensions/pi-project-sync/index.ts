/** Hold subsequent tools until a managed project handoff finishes. */
import { once } from "node:events";
import { createConnection } from "node:net";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

async function project(socket: string, request: unknown) {
  const connection = createConnection({ path: socket });
  try {
    await once(connection, "connect");
    connection.write(JSON.stringify(request) + "\n");
    const records = createInterface({ input: connection, crlfDelay: Infinity });
    try {
      for await (const line of records) {
        const reply = JSON.parse(line);
        if (reply.kind === "owner_failure") throw new Error(reply.error);
        if (reply.kind !== "owner_result" || typeof reply.result?.worktree !== "string") {
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
  const record = globalThis.agentCommsLaunchRecord;
  record.withProject((socket, request) => {
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName === "comms_set_project") return;
    if (resolve(await project(socket, request)) !== resolve(ctx.cwd)) {
      return { block: true, reason: "Your project directory changed. End this turn now; the runtime will automatically continue in the new directory. Do not run more tools in the old project." };
    }
  });
  });
}
