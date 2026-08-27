import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef } from "react";

import type { WorkspaceFileEntry } from "@/api/types";

const VIRTUALIZE_AFTER = 100;

function FileRow({ entry, index, onOpen, onEnter }: {
  entry: WorkspaceFileEntry;
  index: number;
  onOpen: (path: string) => void;
  onEnter: (path: string) => void;
}) {
  return (
    <button
      type="button"
      className="ws-file-row"
      data-file-index={index}
      onClick={() => entry.kind === "directory" ? onEnter(entry.path) : onOpen(entry.path)}
    >
      <span>{entry.kind === "directory" ? "📁" : "📄"}</span>
      <span className="ws-file-name">{entry.name}</span>
      {entry.kind === "file" ? <span className="dim">{entry.size.toLocaleString()} B</span> : null}
    </button>
  );
}

export function WorkspaceFileList({
  entries,
  onOpen,
  onEnter,
}: {
  entries: WorkspaceFileEntry[];
  onOpen: (path: string) => void;
  onEnter: (path: string) => void;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: entries.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 34,
    measureElement: () => 34,
    getItemKey: (index) => entries[index]?.path ?? index,
    overscan: 10,
    initialRect: { width: 320, height: 180 },
  });

  const virtualItems = virtualizer.getVirtualItems();
  const visibleItems = virtualItems.length > 0
    ? virtualItems
    : entries.slice(0, 20).map((_entry, index) => ({ index, key: entries[index]?.path ?? index, start: index * 34 }));

  if (entries.length === 0) return <div className="ws-empty">目录为空</div>;
  if (entries.length <= VIRTUALIZE_AFTER) {
    return (
      <div className="ws-file-list" style={{ overflow: "auto", minHeight: 180, maxHeight: 360 }}>
        {entries.map((entry, index) => (
          <FileRow key={entry.path} entry={entry} index={index} onOpen={onOpen} onEnter={onEnter} />
        ))}
      </div>
    );
  }
  return (
    <div ref={parentRef} className="ws-file-list" style={{ overflow: "auto", minHeight: 180, maxHeight: 360 }}>
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {visibleItems.map((row) => {
          const entry = entries[row.index];
          if (!entry) return null;
          return (
            <button
              type="button"
              className="ws-file-row"
              key={entry.path}
              data-index={row.index}
              data-file-index={row.index}
              ref={virtualizer.measureElement}
              style={{ position: "absolute", width: "100%", transform: `translateY(${row.start}px)` }}
              onClick={() => entry.kind === "directory" ? onEnter(entry.path) : onOpen(entry.path)}
            >
              <span>{entry.kind === "directory" ? "📁" : "📄"}</span>
              <span className="ws-file-name">{entry.name}</span>
              {entry.kind === "file" ? <span className="dim">{entry.size.toLocaleString()} B</span> : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}
