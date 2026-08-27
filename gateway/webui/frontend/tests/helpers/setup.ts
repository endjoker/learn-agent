import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());

class MockEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: MockEventSource[] = [];
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSED = 2;
  readonly url: string;
  readonly withCredentials = false;
  readyState = MockEventSource.OPEN;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }
  close() { this.readyState = MockEventSource.CLOSED; }
  addEventListener() { /* test no-op */ }
  removeEventListener() { /* test no-op */ }
  dispatchEvent() { return true; }
}

Object.defineProperty(globalThis, "EventSource", { value: MockEventSource, configurable: true });

// jsdom 不实现 ResizeObserver；VirtualMessageList（及 tanstack useVirtualizer）依赖它。
class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  readonly targets: Element[] = [];
  readonly callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    MockResizeObserver.instances.push(this);
  }
  observe(target: Element) { this.targets.push(target); }
  unobserve() { /* test no-op */ }
  disconnect() { this.targets.length = 0; }
}
Object.defineProperty(globalThis, "ResizeObserver", { value: MockResizeObserver, configurable: true });

