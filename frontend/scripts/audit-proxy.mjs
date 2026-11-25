// Same-origin proxy for local audits, mimicking production Caddy: /api/* -> backend, everything else -> frontend, and
// the ingress Basic credential header that Caddy forwards (the AST bridge needs it). LOCAL USE ONLY.
import http from "node:http";
const [, , listen = "8080", api = "8000", web = "3000"] = process.argv;
http.createServer((req, res) => {
  const toApi = req.url.startsWith("/api/");
  const headers = { ...req.headers, ...(toApi ? {} : { authorization: "Basic YTpi" }) };
  const up = http.request({ host: "127.0.0.1", port: toApi ? api : web, path: req.url, method: req.method, headers },
    (r) => { res.writeHead(r.statusCode, r.headers); r.pipe(res); });
  up.on("error", () => { res.writeHead(502); res.end("upstream down"); });
  req.pipe(up);
}).listen(Number(listen), "127.0.0.1");
