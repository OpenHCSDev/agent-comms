// One native client lifetime for owner-mediated tools.
// A live owner check may wait behind the coordination database; only the
// connection owner ending the socket makes its outcome unavailable.
import { createConnection } from "node:net";

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
