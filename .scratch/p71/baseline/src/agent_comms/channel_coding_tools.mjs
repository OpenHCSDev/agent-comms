// Pi owns the normal coding tools. The existing owner socket admits each call.
import { requestOwnerTool } from "./owner_tool_socket.mjs";

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
      const response = await requestOwnerTool(
        path,
        request,
        16384,
        "Tool admission outcome UNKNOWN; inspect before retrying.",
      );
      if (response?.ok !== true) return { block: true, reason: response?.error ?? "Coding tool admission denied." };
    } catch (error) {
      return { block: true, reason: error.message };
    }
  });
}
