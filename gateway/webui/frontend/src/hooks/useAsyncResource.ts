import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";

export type AsyncResourceStatus = "idle" | "loading" | "success" | "error";

export interface AsyncResource<T> {
  status: AsyncResourceStatus;
  data: T | undefined;
  error: unknown;
  reload: () => Promise<void>;
  cancel: () => void;
}

export interface AsyncResourceOptions<T> {
  enabled?: boolean;
  initialData?: T;
  keepPreviousData?: boolean;
}

export const useAsyncResource = <T>(
  loader: (signal: AbortSignal) => Promise<T>,
  dependencies: DependencyList,
  options: AsyncResourceOptions<T> = {},
): AsyncResource<T> => {
  const { enabled = true, initialData, keepPreviousData = true } = options;
  const [status, setStatus] = useState<AsyncResourceStatus>(enabled ? "loading" : "idle");
  const [data, setData] = useState<T | undefined>(initialData);
  const [error, setError] = useState<unknown>();
  const generation = useRef(0);
  const active = useRef<AbortController>();
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const cancel = useCallback(() => {
    generation.current += 1;
    active.current?.abort(new DOMException("Obsolete request", "AbortError"));
    active.current = undefined;
  }, []);

  const reload = useCallback(async () => {
    if (!enabled) return;
    cancel();
    const current = generation.current;
    const controller = new AbortController();
    active.current = controller;
    setStatus("loading");
    setError(undefined);
    if (!keepPreviousData) setData(undefined);
    try {
      const next = await loaderRef.current(controller.signal);
      if (controller.signal.aborted || current !== generation.current) return;
      setData(next);
      setStatus("success");
    } catch (nextError) {
      if (controller.signal.aborted || current !== generation.current) return;
      setError(nextError);
      setStatus("error");
    } finally {
      if (active.current === controller) active.current = undefined;
    }
  }, [cancel, enabled, keepPreviousData]);

  useEffect(() => {
    if (!enabled) {
      cancel();
      setStatus("idle");
      return undefined;
    }
    void reload();
    return cancel;
    // The caller explicitly owns the dependency list, like useEffect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, reload, cancel, ...dependencies]);

  return { status, data, error, reload, cancel };
};
