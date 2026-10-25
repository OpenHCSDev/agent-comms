// One native client lifetime for owner-mediated tools.
// A live owner check may wait behind the coordination database; only the
// connection owner ending the socket makes its outcome unavailable.
import { createConnection } from "node:net";

const NOT_STARTED = "Tool owner is unavailable; no tool was started.";

class UnavailableOwnerToolConnection {
  same(other) { return other === this; }

  request() {
    return Promise.reject(new Error(NOT_STARTED));
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

  request(payload, maxRequest, maxResponse, unavailable) {
    const request = Buffer.from(JSON.stringify({ ...payload, token: this.#token }) + "\n", "utf8");
    if (request.length > maxRequest) throw new Error("Native tool request exceeds bound");
    return requestOwnerTool(this.#path, request, maxResponse, unavailable);
  }
}

export function requestOwnerTool(path, request, maxResponse, unavailable) {
  return new Promise((resolve, reject) => {
    const socket = createConnection(path);
    let received = Buffer.alloc(0);
    // Until the request is written the owner cannot have claimed anything; only a
    // failure after the write leaves the claim's outcome unknown.
    let outcome = NOT_STARTED;
    const fail = () => {
      socket.destroy();
      reject(new Error(outcome));
    };
    socket.once("error", fail);
    socket.once("connect", () => {
      outcome = unavailable;
      socket.write(request);
    });
    socket.on("data", (part) => {
      received = Buffer.concat([received, part]);
      if (received.length > maxResponse) return fail();
      if (received.includes(10)) {
        socket.end();
        try {
          resolve(JSON.parse(received.toString("utf8")));
        } catch {
          fail();
        }
      }
    });
    socket.once("end", () => {
      if (!received.includes(10)) fail();
    });
  });
}
