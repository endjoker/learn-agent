import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncActionState<T> {
  pending: boolean;
  data: T | undefined;
  error: unknown;
  run: (...args: never[]) => Promise<T | undefined>;
  reset: () => void;
}

export const useAsyncAction = <TArgs extends unknown[], TResult>(
  action: (signal: AbortSignal, ...args: TArgs) => Promise<TResult>,
): Omit<AsyncActionState<TResult>, "run"> & { run: (...args: TArgs) => Promise<TResult | undefined> } => {
  const [pending, setPending] = useState(false);
  const [data, setData] = useState<TResult>();
  const [error, setError] = useState<unknown>();
  const active = useRef<AbortController>();
  const actionRef = useRef(action);
  actionRef.current = action;

  const reset = useCallback(() => {
    active.current?.abort(new DOMException("Reset", "AbortError"));
    active.current = undefined;
    setPending(false);
    setData(undefined);
    setError(undefined);
  }, []);

  const run = useCallback(async (...args: TArgs) => {
    active.current?.abort(new DOMException("Superseded", "AbortError"));
    const controller = new AbortController();
    active.current = controller;
    setPending(true);
    setError(undefined);
    try {
      const result = await actionRef.current(controller.signal, ...args);
      if (!controller.signal.aborted) setData(result);
      return controller.signal.aborted ? undefined : result;
    } catch (nextError) {
      if (!controller.signal.aborted) setError(nextError);
      return undefined;
    } finally {
      if (active.current === controller) {
        active.current = undefined;
        setPending(false);
      }
    }
  }, []);

  useEffect(() => reset, [reset]);
  return { pending, data, error, run, reset };
};
