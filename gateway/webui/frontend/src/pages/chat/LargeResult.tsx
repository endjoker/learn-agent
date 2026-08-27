import { useState } from "react";

import { chatDerivedCache, estimateUtf8Bytes } from "@/pages/chat/byteLru";

export const LARGE_RESULT_PREVIEW_BYTES = 64 * 1024;

const utf8Slice = (value: string, maxBytes: number): string => {
  const encoder = new TextEncoder();
  if (encoder.encode(value).byteLength <= maxBytes) return value;
  let low = 0; let high = value.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (encoder.encode(value.slice(0, mid)).byteLength <= maxBytes) low = mid;
    else high = mid - 1;
  }
  return value.slice(0, low);
};

export function LargeResult({ cacheKey, value }: { cacheKey: string; value: string }) {
  const bytes = estimateUtf8Bytes(value);
  const truncated = bytes > LARGE_RESULT_PREVIEW_BYTES;
  const [expanded, setExpanded] = useState(() => chatDerivedCache.get(cacheKey) === value);
  const visible = expanded || !truncated ? value : utf8Slice(value, LARGE_RESULT_PREVIEW_BYTES);
  const toggle = () => {
    if (expanded) chatDerivedCache.delete(cacheKey);
    else chatDerivedCache.set(cacheKey, value, bytes);
    setExpanded(!expanded);
  };
  return (
    <div className="large-result-wrap">
      <pre data-testid="large-result">{visible}</pre>
      {truncated ? (
        <div className="large-result-controls">
          {!expanded ? <span>仅显示前 64 KiB，共 {bytes.toLocaleString()} bytes</span> : null}
          <button className="btn" type="button" onClick={toggle}>{expanded ? "收起" : "展开全部"}</button>
        </div>
      ) : null}
    </div>
  );
}
