// One native client lifetime for owner-mediated tools.
// A live owner check may wait behind the coordination database; only the
// connection owner ending the socket makes its outcome unavailable.
import { createConnection } from "node:net";

// Whether the request was written decides whether a retry is safe, so each
// answer is its own class; callers never read it out of the message.
class OwnerToolFailure extends Error {
  get name() { return this.constructor.name; }
}

export class ToolNotStarted extends OwnerToolFailure {
  constructor() { super("Tool owner is unavailable; no tool was started."); }
}

export class ToolOutcomeUnknown extends OwnerToolFailure {
  constructor() { super("Tool claim outcome UNKNOWN; inspect before retrying."); }
}

class UnavailableOwnerToolConnection {
  same(other) { return other === this; }

  request() {
    return Promise.reject(new ToolNotStarted());
  }

  execute() {
    return Promise.reject(new ToolNotStarted());
  }
}

export class OwnerToolConnection {
  static unavailable = Object.freeze(new UnavailableOwnerToolConnection());

  static capture(value) {
    if (value === undefined) return this.unavailable;
    if (!value || Object.keys(value).sort().join(",") !== "path,token" ||
        typeof value.path !== "string" || !value.path ||
        typeof value.token !== "string" || !/^[0-9a-f]{64}$/.test(value.token)) {
      throw new TypeError("Invalid per-input tool connection");
    }
    return new this(value.path, value.token);
  }

  #path;
  #token;

  constructor(path, token) {
    this.#path = path;
    this.#token = token;
  }

  same(other) {
    return other instanceof OwnerToolConnection && this.#path === other.#path && this.#token === other.#token;
  }

  request(payload, maxRequest, maxResponse) {
    const request = Buffer.from(JSON.stringify({ ...payload, token: this.#token }) + "\n", "utf8");
    if (request.length > maxRequest) throw new Error("Native tool request exceeds bound");
    return requestOwnerTool(this.#path, request, maxResponse);
  }

  async execute(callId, name, args) {
    const response = await this.request(
      { call_id: callId, name, arguments: args, operation: { kind: "execute" } },
      1024 * 1024,
      64 * 1024 * 1024,
    );
    if (response?.ok !== true) throw new Error(response?.error ?? "Core tool execution failed.");
    if (!response.result || typeof response.result !== "object" || Array.isArray(response.result)) {
      throw new ToolOutcomeUnknown();
    }
    return response.result;
  }
}

export function requestOwnerTool(path, request, maxResponse) {
  return new Promise((resolve, reject) => {
    const socket = createConnection(path);
    const chunks = [];
    let receivedBytes = 0;
    let complete = false;
    // Until the request is written the owner cannot have claimed anything; only a
    // failure after the write leaves the claim's outcome unknown.
    let failure = ToolNotStarted;
    const fail = () => {
      socket.destroy();
      reject(new failure());
    };
    socket.once("error", fail);
    socket.once("connect", () => {
      failure = ToolOutcomeUnknown;
      socket.write(request);
    });
    socket.on("data", (part) => {
      chunks.push(part);
      receivedBytes += part.length;
      if (receivedBytes > maxResponse) return fail();
      if (part.includes(10)) {
        complete = true;
        socket.end();
        try {
          resolve(JSON.parse(Buffer.concat(chunks, receivedBytes).toString("utf8")));
        } catch {
          fail();
        }
      }
    });
    socket.once("end", () => {
      if (!complete) fail();
    });
  });
}
