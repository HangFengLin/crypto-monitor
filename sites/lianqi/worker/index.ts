import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";

interface Env {
  ASSETS: { fetch(request: Request): Promise<Response> };
  LIANQI_ORIGIN_URL?: string;
  IMAGES: {
    input(stream: ReadableStream): {
      transform(options: Record<string, unknown>): {
        output(options: { format: string; quality: number }): Promise<{ response(): Response }>;
      };
    };
  };
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

const PROXY_PATHS = ["/api/", "/reports/"];

async function proxyToLianqi(request: Request, env: Env) {
  const incomingUrl = new URL(request.url);
  if (!env.LIANQI_ORIGIN_URL) throw new Error("LIANQI_ORIGIN_URL is required");
  const origin = env.LIANQI_ORIGIN_URL.replace(/\/$/, "");
  const targetUrl = new URL(`${incomingUrl.pathname}${incomingUrl.search}`, `${origin}/`);
  const headers = new Headers(request.headers);
  ["authorization", "cookie", "host", "oai-authenticated-user-id", "oai-authenticated-user-email", "oai-authenticated-user-full-name"].forEach((name) => headers.delete(name));
  headers.set("accept-encoding", "identity");

  const upstream = new Request(targetUrl, {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
    redirect: "follow",
  });
  const response = await fetch(upstream);
  const responseHeaders = new Headers(response.headers);
  responseHeaders.set("cache-control", "no-store");
  responseHeaders.delete("content-length");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers: responseHeaders });
}

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (PROXY_PATHS.some((prefix) => url.pathname.startsWith(prefix))) {
      try {
        return await proxyToLianqi(request, env);
      } catch {
        return Response.json({ ok: false, error: "炼气数据服务暂时不可用" }, { status: 502 });
      }
    }

    if (url.pathname === "/_vinext/image") {
      const allowedWidths = [...DEFAULT_DEVICE_SIZES, ...DEFAULT_IMAGE_SIZES];
      return handleImageOptimization(request, {
        fetchAsset: (path) => env.ASSETS.fetch(new Request(new URL(path, request.url))),
        transformImage: async (body, { width, format, quality }) => {
          const result = await env.IMAGES.input(body).transform(width > 0 ? { width } : {}).output({ format, quality });
          return result.response();
        },
      }, allowedWidths);
    }

    return handler.fetch(request, env, ctx);
  },
};

export default worker;
