import type { TuiPlugin, TuiPluginApi } from "@opencode-ai/plugin/tui"
import { createEffect, createRoot, on } from "solid-js"
import { spawn, type ChildProcess } from "node:child_process"
import { open, mkdir } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"

/** One selected session, one owned bridge. Switching joins the old child first. */
class SessionBridge {
  private child?: ChildProcess
  private stopped: Promise<void> = Promise.resolve()
  private transitions: Promise<void> = Promise.resolve()

  constructor(private api: TuiPluginApi, private server: string) {}

  select(session: string | undefined): Promise<void> {
    this.transitions = this.transitions.then(async () => {
      this.child?.kill("SIGTERM")
      await this.stopped
      this.child = undefined
      if (!session || this.api.lifecycle.signal.aborted) return
      const state = join(homedir(), ".local/state/opencode-comms-inbox", session)
      await mkdir(state, { recursive: true, mode: 0o700 })
      const log = await open(join(state, "bridge.log"), "a", 0o600)
      const child = spawn("agent-comms-opencode-bridge", [
        "attach", "--server", this.server, "--directory", this.api.state.path.directory,
        "--session", session, "--pid", String(process.pid),
      ], { detached: true, stdio: ["ignore", log.fd, log.fd] })
      this.child = child
      this.stopped = new Promise<void>((resolve, reject) => {
        child.once("error", reject)
        child.once("exit", (code, signal) => {
          if (code !== 0 && signal !== "SIGTERM") {
            this.api.ui.toast({ title: "Comms bridge stopped", message: `${code ?? signal}; see ${state}/bridge.log`, variant: "error" })
          }
          resolve()
        })
      })
      await log.close()
      this.stopped.catch(error => this.failure(error))
    })
    return this.transitions
  }

  failure(error: unknown): void {
    this.api.ui.toast({ title: "Comms connection failed", message: String(error), variant: "error" })
  }

  async dispose(): Promise<void> {
    await this.select(undefined)
  }
}

const tui: TuiPlugin = async api => {
  const health = await api.client.global.health({ throwOnError: true })
  const server = new URL(health.request.url)
  if (server.hostname === "opencode.internal") {
    api.ui.toast({ title: "Comms needs a local listener", message: "Start with opencode-comms; it chooses the port automatically.", variant: "warning" })
    return
  }
  const bridge = new SessionBridge(api, server.origin)
  const dispose = createRoot(dispose => {
    createEffect(on(() => {
      const route = api.route.current
      // Decode the public route union once; custom plugin routes also have params.
      if (route.name !== "session") return undefined
      const session = route.params?.sessionID
      if (typeof session !== "string") throw new Error("Native session route has no sessionID")
      return session
    }, session => { bridge.select(session).catch(error => bridge.failure(error)) }))
    return dispose
  })
  api.lifecycle.onDispose(async () => { dispose(); await bridge.dispose() })
}

export default { id: "agent-comms", tui }
