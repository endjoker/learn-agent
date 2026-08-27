import type { WorkspaceFileResponse } from "@/api/types";
import { LargeResult } from "@/pages/chat/LargeResult";

export function WorkspaceFileViewer({ file }: { file: WorkspaceFileResponse }) {
  return (
    <section className="ws-file-viewer" aria-label="文件查看">
      <header className="ws-file-viewer-head">
        <strong>{file.path}</strong>
        <span className="dim">{file.size.toLocaleString()} B{file.truncated ? " · 服务端已截断" : ""}</span>
      </header>
      <LargeResult cacheKey={`workspace:${file.workspace_id}:file:${file.path}`} value={file.content} />
    </section>
  );
}
