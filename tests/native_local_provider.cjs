// Keep the actual native CLI/RPC; redirect provider config to the local test server.
const fs = require('node:fs');
const net = require('node:net');
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
    const opts = Array.isArray(args[0]) ? args[0][0] : net._normalizeArgs(args)[0];
    if (opts.port && !['127.0.0.1', 'localhost', '::1'].includes(opts.host)) {
        throw Error('BLOCKED_NONLOCAL_NETWORK: ' + opts.host);
    }
    return connect.apply(this, args);
};
fs.writeFileSync(process.env.PI_CODING_AGENT_DIR + '/models.json', JSON.stringify({
    providers: {openrouter: {baseUrl: process.env.AC_NATIVE_LOCAL_PROVIDER_URL}},
}));
