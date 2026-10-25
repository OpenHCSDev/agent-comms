// Pi owns the normal coding tools. The existing owner socket allows each call.
export default function channelCodingTools(pi) {
  const refusals = new Map();
  pi.on("session_shutdown", () => refusals.clear());
  // Blocked calls skip tool_result. message_end runs before the SDK commits
  // its canonical result object, including those blocked before execution.
  pi.on("message_end", (event) => {
    const details = refusals.get(event.message.toolCallId);
    refusals.delete(event.message.toolCallId);
    if (details) return { message: { ...event.message, details } };
  });
  pi.on("tool_call", async (event, context) => {
    try {
      const response = await context.ownerTools.request(
        { call_id: event.toolCallId, name: event.toolName, arguments: event.input },
        1024 * 1024,
        16384,
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
