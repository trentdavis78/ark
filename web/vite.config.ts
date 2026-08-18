import { defineConfig } from "vite";
import { extractHandler } from "./src/server/extract";

/**
 * The vision endpoint runs server-side because the Anthropic API key must
 * never reach the browser. In production this handler deploys to any
 * fetch-standard runtime (Supabase Edge Functions, Vercel, Cloudflare); in
 * dev it is mounted straight into Vite so there is one code path, not two.
 */
export default defineConfig({
  plugins: [
    {
      name: "blacktop-extract-api",
      configureServer(server) {
        server.middlewares.use("/api/extract", async (req, res) => {
          const chunks: Buffer[] = [];
          for await (const chunk of req) chunks.push(chunk as Buffer);
          const request = new Request("http://local/api/extract", {
            method: req.method ?? "POST",
            headers: req.headers as HeadersInit,
            body: chunks.length ? Buffer.concat(chunks) : null,
          });
          const response = await extractHandler(request);
          res.statusCode = response.status;
          response.headers.forEach((v, k) => res.setHeader(k, v));
          res.end(await response.text());
        });
      },
    },
  ],
  build: { target: "es2022", sourcemap: true },
});
