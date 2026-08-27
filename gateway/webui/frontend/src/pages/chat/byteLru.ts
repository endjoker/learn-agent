export const DEFAULT_CHAT_CACHE_BYTES = 64 * 1024 * 1024;

export const estimateUtf8Bytes = (value: string): number => new TextEncoder().encode(value).byteLength;

interface CacheEntry<T> {
  value: T;
  bytes: number;
}

/**
 * Byte-budgeted LRU for derived/expanded tool output.
 *
 * Values are charged by a caller-supplied estimate or UTF-8 string bytes. Reads
 * refresh recency; inserts evict oldest entries until within budget. Oversized
 * entries are never retained. Session switches call deletePrefix(), while a
 * complete UI teardown calls clear(). Replacing a key invalidates its old size.
 */
export class ByteLruCache<T> {
  private readonly entries = new Map<string, CacheEntry<T>>();
  private bytes = 0;

  constructor(readonly maxBytes = DEFAULT_CHAT_CACHE_BYTES) {
    if (!Number.isFinite(maxBytes) || maxBytes < 0) throw new RangeError("maxBytes must be non-negative");
  }

  get totalBytes(): number { return this.bytes; }
  get size(): number { return this.entries.size; }

  get(key: string): T | undefined {
    const entry = this.entries.get(key);
    if (!entry) return undefined;
    this.entries.delete(key);
    this.entries.set(key, entry);
    return entry.value;
  }

  set(key: string, value: T, estimatedBytes?: number): void {
    const prior = this.entries.get(key);
    if (prior) {
      this.bytes -= prior.bytes;
      this.entries.delete(key);
    }
    const bytes = Math.max(0, Math.ceil(estimatedBytes ?? this.defaultEstimate(value)));
    if (bytes > this.maxBytes) return;
    this.entries.set(key, { value, bytes });
    this.bytes += bytes;
    while (this.bytes > this.maxBytes) {
      const oldest = this.entries.keys().next().value;
      if (oldest === undefined) break;
      this.delete(oldest);
    }
  }

  delete(key: string): boolean {
    const entry = this.entries.get(key);
    if (!entry) return false;
    this.bytes -= entry.bytes;
    return this.entries.delete(key);
  }

  deletePrefix(prefix: string): void {
    for (const key of [...this.entries.keys()]) if (key.startsWith(prefix)) this.delete(key);
  }

  clear(): void {
    this.entries.clear();
    this.bytes = 0;
  }

  private defaultEstimate(value: T): number {
    if (typeof value === "string") return estimateUtf8Bytes(value);
    try { return estimateUtf8Bytes(JSON.stringify(value)); }
    catch { return 0; }
  }
}

export const chatDerivedCache = new ByteLruCache<string>();
