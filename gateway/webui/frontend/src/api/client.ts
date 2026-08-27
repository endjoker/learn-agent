import type { ApiErrorPayload } from "@/api/types";

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
  query?: Record<string, QueryValue>;
  headers?: HeadersInit;
  silent?: boolean;
}

export interface ApiClientOptions {
  baseUrl?: string;
  timeoutMs?: number;
  fetcher?: typeof fetch;
  headers?: HeadersInit;
  onError?: (error: ApiError, options: RequestOptions) => void;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly details?: unknown;
  readonly payload?: unknown;

  constructor(message: string, options: { status: number; code?: string; details?: unknown; payload?: unknown }) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.details = options.details;
    this.payload = options.payload;
  }
}

export class TimeoutError extends DOMException {
  constructor(message = "Request timed out") {
    super(message, "TimeoutError");
  }
}

const appendQuery = (path: string, query?: Record<string, QueryValue>): string => {
  if (!query) return path;
  // 只在第一个 "?" 处切分：pathname 之外的部分整体作为已有查询串（其中可含
  // 字面 "?"，属于 value 的一部分），避免多 "?" 时把第二个 "?" 误当分隔符。
  const idx = path.indexOf("?");
  const pathname = idx === -1 ? path : path.slice(0, idx);
  const existing = idx === -1 ? "" : path.slice(idx + 1);
  const params = new URLSearchParams(existing);
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(key, String(value));
  }
  const suffix = params.toString();
  // 无新增参数：原样返回（保留已有查询串，避免丢失）
  if (!suffix) return path;
  return `${pathname}?${suffix}`;
};

const mergeSignals = (callerSignal: AbortSignal | undefined, timeoutMs: number) => {
  const controller = new AbortController();
  let callerAbort: (() => void) | undefined;
  if (callerSignal) {
    callerAbort = () => controller.abort(callerSignal.reason ?? new DOMException("Aborted", "AbortError"));
    if (callerSignal.aborted) callerAbort();
    else callerSignal.addEventListener("abort", callerAbort, { once: true });
  }
  const timer = timeoutMs > 0
    ? globalThis.setTimeout(() => controller.abort(new TimeoutError()), timeoutMs)
    : undefined;
  return {
    signal: controller.signal,
    cleanup: () => {
      if (timer !== undefined) globalThis.clearTimeout(timer);
      if (callerSignal && callerAbort) callerSignal.removeEventListener("abort", callerAbort);
    },
  };
};

const parseResponse = async (response: Response): Promise<unknown> => {
  if (response.status === 204) return undefined;
  const contentType = response.headers.get("Content-Type") ?? "";
  if (contentType.toLowerCase().includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return undefined;
    }
  }
  return response.text();
};

export interface ApiClient {
  request<T>(method: HttpMethod, path: string, body?: unknown, options?: RequestOptions): Promise<T>;
  get<T>(path: string, options?: RequestOptions): Promise<T>;
  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>;
  put<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>;
  patch<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>;
  delete<T>(path: string, options?: RequestOptions): Promise<T>;
}

export const createApiClient = (clientOptions: ApiClientOptions = {}): ApiClient => {
  const fetcher = clientOptions.fetcher ?? globalThis.fetch.bind(globalThis);
  const baseUrl = clientOptions.baseUrl?.replace(/\/$/, "") ?? "";
  const defaultTimeout = clientOptions.timeoutMs ?? 30_000;

  const request = async <T>(
    method: HttpMethod,
    path: string,
    body?: unknown,
    options: RequestOptions = {},
  ): Promise<T> => {
    const timeout = mergeSignals(options.signal, options.timeoutMs ?? defaultTimeout);
    const headers: Record<string, string> = {};
    new Headers(clientOptions.headers).forEach((value, key) => { headers[key] = value; });
    new Headers(options.headers).forEach((value, key) => { headers[key] = value; });
    let requestBody: BodyInit | undefined;
    if (body !== undefined) {
      if (body instanceof FormData || body instanceof Blob || typeof body === "string") requestBody = body;
      else {
        headers["Content-Type"] = "application/json";
        requestBody = JSON.stringify(body);
      }
    }

    try {
      const response = await fetcher(`${baseUrl}${appendQuery(path, options.query)}`, {
        method,
        body: requestBody,
        headers,
        signal: timeout.signal,
      });
      const payload = await parseResponse(response);
      if (!response.ok) {
        const errorPayload = payload && typeof payload === "object" ? payload as ApiErrorPayload : undefined;
        throw new ApiError(errorPayload?.error || response.statusText || `HTTP ${response.status}`, {
          status: response.status,
          code: errorPayload?.code,
          details: errorPayload?.details,
          payload,
        });
      }
      return payload as T;
    } catch (error) {
      if (!options.silent) {
        if (error instanceof ApiError) {
          clientOptions.onError?.(error, options);
        } else if (error instanceof TimeoutError) {
          // 超时（内部 abort 触发）也上报 onError（status 0 表示传输层失败）
          clientOptions.onError?.(new ApiError(error.message, { status: 0 }), options);
        } else if (error instanceof TypeError) {
          // fetch 网络层异常（断网 / DNS / 连接拒绝）也上报 onError
          clientOptions.onError?.(new ApiError(error.message, { status: 0 }), options);
        }
      }
      throw error;
    } finally {
      timeout.cleanup();
    }
  };

  return {
    request,
    get: <T>(path: string, options?: RequestOptions) => request<T>("GET", path, undefined, options),
    post: <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>("POST", path, body, options),
    put: <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>("PUT", path, body, options),
    patch: <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>("PATCH", path, body, options),
    delete: <T>(path: string, options?: RequestOptions) => request<T>("DELETE", path, undefined, options),
  };
};

export const api = createApiClient();
