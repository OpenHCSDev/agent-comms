const request = JSON.parse(process.argv[1]);
console.log(JSON.stringify({answer:request.text.toUpperCase(), ...(request.extra ? {unowned:1} : {})}));
