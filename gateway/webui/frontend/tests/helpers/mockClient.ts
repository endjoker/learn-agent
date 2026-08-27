import { vi } from "vitest";

import type { ApiClient } from "@/api/client";

type Handler = (path: string, body?: unknown, options?: unknown) => Promise<unknown>;

export interface MockClientRoutes {
  get?: Handler;
  post?: Handler;
  put?: Handler;
  patch?: Handler;
  delete?: Handler;
  request?: Handler;
}

export interface MockClient extends ApiClient {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
  patch: ReturnType<typeof vi.fn>;
  delete: ReturnType<typeof vi.fn>;
  request: ReturnType<typeof vi.fn>;
}

const rejectUnstubbed = (method: string): Handler => () => Promise.reject(new Error(`${method} not stubbed`));

export const createMockClient = (routes: MockClientRoutes = {}): MockClient => {
  // 所有方法默认 reject("not stubbed")：默认成功会掩盖漏 stub 的写入路径
  // （POST/PUT/PATCH/DELETE 静默 ok:true），与 GET 一致显式声明每个路由。
  const client = {
    get: vi.fn(routes.get ?? rejectUnstubbed("GET")),
    post: vi.fn(routes.post ?? rejectUnstubbed("POST")),
    put: vi.fn(routes.put ?? rejectUnstubbed("PUT")),
    patch: vi.fn(routes.patch ?? rejectUnstubbed("PATCH")),
    delete: vi.fn(routes.delete ?? rejectUnstubbed("DELETE")),
    request: vi.fn(routes.request ?? rejectUnstubbed("REQUEST")),
  };
  return client as unknown as MockClient;
};
