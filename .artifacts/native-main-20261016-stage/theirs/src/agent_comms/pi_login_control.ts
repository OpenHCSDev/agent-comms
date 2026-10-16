/** Login-only control adapter. Pi's existing /login UI owns all authentication. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const provider = process.env.AGENT_COMMS_LOGIN_PROVIDER || "";
  let timer: ReturnType<typeof setInterval> | undefined;
  pi.on("session_start", (_event, ctx) => {
    ctx.ui.setEditorText(`/login${provider ? ` ${provider}` : ""}`);
    ctx.ui.notify("Press Enter to sign in using Pi. Ctrl+D closes this login session.", "info");
    if (provider && !ctx.modelRegistry.getProviderAuthStatus(provider).configured) {
      timer = setInterval(() => {
        if (ctx.modelRegistry.getProviderAuthStatus(provider).configured) {
          clearInterval(timer);
          ctx.shutdown();
        }
      }, 300);
    }
  });
  pi.on("input", (_event, ctx) => {
    ctx.ui.notify("This is a provider-login session. Use /login or /logout, then Ctrl+D.", "info");
    return { action: "handled" };
  });
  pi.on("session_shutdown", () => clearInterval(timer));
}
