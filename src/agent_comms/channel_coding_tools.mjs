// Pi owns the normal coding tools. The existing owner socket allows each call.
export default function channelCodingTools(pi) {
  pi.on("tool_call", async (event, context) => {
    try {
      const response = await context.ownerTools.request(
        { call_id: event.toolCallId, name: event.toolName, arguments: event.input },
        1024 * 1024,
        16384,
        "Tool claim outcome UNKNOWN; inspect before retrying.",
      );
      if (response?.ok !== true) return { block: true, reason: response?.error ?? "Coding tool claim denied." };
    } catch (error) {
      return { block: true, reason: error.message };
    }
  });
}
