// Pi owns the normal coding tools. The existing owner socket admits each call.
import { createConnection } from "node:net";

export default function channelCodingTools(pi) {
  pi.on("tool_call", async (event) => {
    const path = process.env.AGENT_COMMS_SELECTED_TOOL_SOCKET;
    const token = process.env.AGENT_COMMS_SELECTED_TOOL_TOKEN;
    if (!path || !/^[0-9a-f]{64}$/.test(token ?? "")) {
      return { block: true, reason: "Channel coding owner is unavailable; no tool was started." };
    }
    const request = Buffer.from(JSON.stringify({ token, call_id: event.toolCallId,
      name: event.toolName, arguments: event.input }) + "\n", "utf8");
    if (request.length > 1024 * 1024) return { block: true, reason: "Coding tool request exceeds the native RPC record bound." };
    try {
      const response = await new Promise((resolve, reject) => {
        const socket = createConnection(path);
        let received = Buffer.alloc(0);
        const fail = () => { socket.destroy(); reject(new Error("Tool admission outcome UNKNOWN; inspect before retrying.")); };
        socket.setTimeout(12000, fail);
        socket.once("error", fail);
        socket.once("connect", () => socket.write(request));
        socket.on("data", (part) => {
          received = Buffer.concat([received, part]);
          if (received.length > 16384) return fail();
          if (received.includes(10)) {
            socket.end();
            try { resolve(JSON.parse(received.toString("utf8"))); } catch { fail(); }
          }
        });
        socket.once("end", () => { if (!received.includes(10)) fail(); });
      });
      if (response?.ok !== true) return { block: true, reason: response?.error ?? "Coding tool admission denied." };
    } catch (error) {
      return { block: true, reason: error.message };
    }
  });
}
