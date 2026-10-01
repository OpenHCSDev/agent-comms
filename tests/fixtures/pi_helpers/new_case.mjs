import {readFileSync} from "node:fs";
const request = JSON.parse(readFileSync(0, 'utf8'));
console.log(JSON.stringify({answer:request.text.slice(0, 8).toUpperCase(), ...(request.extra ? {unowned:1} : {})}));
