/**
 * Local dev server for the vision endpoint.
 *
 * Production deploys `extractHandler` to any fetch-standard runtime (Supabase
 * Edge Functions, Vercel, Cloudflare). This exists so the Android client can
 * be pointed at a laptop during development:
 *
 *   ANDROID: EXTRACT_ENDPOINT = "http://<laptop-ip>:8787/api/extract"
 */

import { createServer } from "node:http";
import { extractHandler } from "./extract";

const PORT = Number(process.env["PORT"] ?? 8787);

createServer(async (req, res) => {
  if (!req.url?.startsWith("/api/extract")) {
    res.writeHead(404).end("not found");
    return;
  }
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  const response = await extractHandler(
    new Request("http://local/api/extract", {
      method: req.method ?? "POST",
      headers: req.headers as HeadersInit,
      body: chunks.length ? Buffer.concat(chunks) : null,
    }),
  );
  const headers: Record<string, string> = {};
  response.headers.forEach((value, key) => {
    headers[key] = value;
  });
  res.writeHead(response.status, headers);
  res.end(await response.text());
}).listen(PORT, () => {
  console.log(`vision endpoint on http://0.0.0.0:${PORT}/api/extract`);
});
