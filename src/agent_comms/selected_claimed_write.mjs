// Explicit, separately gated native Pi tool. This process never writes a file.
// The owner supplies and verifies all wake/attempt authority across private IPC.
import { createConnection } from "node:net";

const TOOL = "selected_claimed_write";
const MAX_CONTENT = 128 * 1024; // Existing native RPC records are limited to 1 MiB.
const MAX_RESPONSE = 4096;

export default function registerSelectedClaimedWrite(pi) {
  pi.registerTool({
    name: TOOL,
    label: "Owner-mediated existing-file replacement",
    description: "Request one bounded UTF-8 replacement of an existing worktree file. You may make one request; the owner independently verifies the selected wake and resource claim before any write. Your request grants no authority. No create, shell, generic edit, or retry on uncertainty.",
    parameters: {
      type: "object",
      properties: {
        resource: { type: "string", description: "Relative existing worktree file path (no .. or symlinks)" },
        contents: { type: "string", description: "Complete UTF-8 replacement content (at most 128 KiB)" },
      },
      required: ["resource", "contents"],
      additionalProperties: false,
    },
    async execute(callId, params, signal) {
      if (signal?.aborted) throw new Error("Selected tool cancelled; no retry on uncertainty");
      if (!params || Object.keys(params).sort().join(",") !== "contents,resource" ||
          typeof params.resource !== "string" || typeof params.contents !== "string" ||
          Buffer.byteLength(params.contents, "utf8") > MAX_CONTENT ||
          /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(params.contents)) {
        throw new Error("Selected tool arguments are invalid or oversized");
      }
      const path = process.env.AGENT_COMMS_SELECTED_TOOL_SOCKET;
      const token = process.env.AGENT_COMMS_SELECTED_TOOL_TOKEN;
      if (!path || !/^[0-9a-f]{64}$/.test(token ?? "")) throw new Error("Selected tool owner bridge unavailable");
      const request = Buffer.from(JSON.stringify({ token, request: { call_id: callId, arguments: params } }) + "\n", "utf8");
      if (request.byteLength > MAX_CONTENT + 8192) throw new Error("Selected tool request exceeds bound");
      const response = await new Promise((resolve, reject) => {
        const socket = createConnection(path);
        let received = Buffer.alloc(0);
        const fail = () => { socket.destroy(); reject(new Error("Selected tool outcome UNKNOWN; no retry")); };
        socket.setTimeout(12000, fail);
        socket.once("error", fail);
        socket.on("data", (part) => {
          received = Buffer.concat([received, part]);
          if (received.length > MAX_RESPONSE) return fail();
          if (received.includes(10)) {
            socket.end();
            try { resolve(JSON.parse(received.toString("utf8"))); } catch { fail(); }
          }
        });
        socket.once("connect", () => socket.write(request));
        socket.once("end", () => { if (!received.includes(10)) fail(); });
      });
      if (response?.ok !== true) throw new Error("Selected tool denied or outcome UNKNOWN; no retry");
      return { content: [{ type: "text", text: "Owner committed one selected existing-file replacement." }], details: {} };
    },
  });
}
