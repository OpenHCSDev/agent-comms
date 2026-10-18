// One native client lifetime for owner-mediated tools.
// A live owner check may wait behind the coordination database; only the
// connection owner ending the socket makes its outcome unavailable.
import { createConnection } from "node:net";

class NoToolOwner {
  request() { throw new Error("Tool owner is unavailable; no tool was started."); }
}

class SocketToolOwner {
  constructor(binding) {
    if (typeof binding.path !== "string" || !binding.path.startsWith("/") ||
        !/^[0-9a-f]{64}$/.test(binding.token ?? "")) {
      throw new Error("Tools require an authenticated owner endpoint");
    }
    this.path = binding.path;
    this.token = binding.token;
  }
  request(fields, maxResponse, unavailable, maxRequest) {
    const request = Buffer.from(JSON.stringify({ ...fields, token: this.token }) + "\n");
    if (request.length > maxRequest) throw new Error("Tool request exceeds the record bound");
    return requestOwnerTool(this.path, request, maxResponse, unavailable);
  }
}

let owner = new NoToolOwner();

export function configureOwnerTools(binding) {
  switch (binding?.kind) {
    case "none": owner = new NoToolOwner(); break;
    case "socket": owner = new SocketToolOwner(binding); break;
    default: throw new Error("Invalid tool owner binding");
  }
}

export function requestConfiguredTool(fields, maxResponse, unavailable, maxRequest = 1024 * 1024) {
  return owner.request(fields, maxResponse, unavailable, maxRequest);
}

export function requestOwnerTool(path, request, maxResponse, unavailable) {
  return new Promise((resolve, reject) => {
    const socket = createConnection(path);
    let received = Buffer.alloc(0);
    const fail = () => {
      socket.destroy();
      reject(new Error(unavailable));
    };
    socket.once("error", fail);
    socket.once("connect", () => socket.write(request));
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
