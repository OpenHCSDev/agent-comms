// Pi owns the normal coding tools. The existing owner socket allows each call.
export default function channelCodingTools(pi) {
  const refusals = new Map();
  pi.on("session_shutdown", () => refusals.clear());
  pi.on("tool_result", (event) => {
    const details = refusals.get(event.toolCallId);
    refusals.delete(event.toolCallId);
    if (details) return { details, isError: true };
  });
  pi.on("tool_call", async (event, context) => {
    try {
      const response = await context.ownerTools.request(
        { call_id: event.toolCallId, name: event.toolName, arguments: event.input },
        1024 * 1024,
        16384,
        "Tool claim outcome UNKNOWN; inspect before retrying.",
      );
      if (response?.ok !== true) {
        if (response?.details) refusals.set(event.toolCallId, response.details);
        return { block: true, reason: response?.error ?? "Coding tool claim denied." };
      }
    } catch (error) {
      return { block: true, reason: error.message };
    }
  });
}
