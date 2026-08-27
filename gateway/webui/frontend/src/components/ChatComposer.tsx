import { useRef, useState, type ReactNode } from "react";

import { toast } from "@/components/toast";

export interface AttachedFile {
  name: string;
  media_type: string;
  data: string;
  preview: string;
}

export interface ChatCommand {
  name: string;
  args?: string;
  help?: string;
  insert_text?: string;
  client_hint?: string;
}

interface ChatComposerProps {
  commands?: ChatCommand[];
  busy?: boolean;
  placeholder?: string;
  ariaLabel?: string;
  onSend: (text: string, files?: AttachedFile[]) => Promise<void> | void;
  onStop?: () => Promise<void> | void;
  contextSlot?: ReactNode;
  /** 会话运行中且队列有待插项时显示"插入提示"快捷键（复用队列面板 steering 逻辑）。 */
  steeringAvailable?: boolean;
  onSteering?: () => Promise<void> | void;
}

const MAX_FILE_SIZE = 20 * 1024 * 1024;
const ACCEPTED_FILES = "image/*,application/pdf,text/*,.md,.json,.py,.js,.ts,.csv";

export function ChatComposer({
  commands = [],
  busy = false,
  placeholder = "输入消息…（/ 触发命令补全；Ctrl+V 粘贴图片；Enter 发送，Shift+Enter 换行）",
  ariaLabel = "消息",
  onSend,
  onStop,
  contextSlot,
  steeringAvailable = false,
  onSteering,
}: ChatComposerProps) {
  const [input, setInput] = useState("");
  const [files, setFiles] = useState<AttachedFile[]>([]);
  const [acHits, setAcHits] = useState<ChatCommand[] | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const showAutocomplete = (value: string) => {
    if (!value.startsWith("/") || value.includes(" ")) { setAcHits(null); return; }
    const hits = commands.filter((command) => command.name.startsWith(value)).slice(0, 8);
    setAcHits(hits.length ? hits : null);
  };

  const addFile = (file: File) => {
    if (file.size > MAX_FILE_SIZE) { toast(`${file.name} 超过 20MB 限制`, "err"); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      const data = result.includes(",") ? (result.split(",", 2)[1] ?? "") : result;
      setFiles((current) => [...current, {
        name: file.name,
        media_type: file.type || "image/png",
        data,
        preview: result,
      }]);
    };
    reader.readAsDataURL(file);
  };

  const submit = async () => {
    const text = input.trim();
    const attachments = [...files];
    // busy 不再阻止发送：会话运行中输入按"队列"入队（设计方案 8.3），
    // 空闲立即发送、运行中入队均由上层 send() 处理。
    if (!text && attachments.length === 0) return;
    setInput("");
    setFiles([]);
    setAcHits(null);
    if (inputRef.current) inputRef.current.style.height = "auto";
    try {
      await onSend(text, attachments.length ? attachments : undefined);
    } catch {
      setInput(text);
      setFiles(attachments);
    }
  };

  const pickCommand = (command: ChatCommand) => {
    setInput(command.insert_text || `${command.name} `);
    setAcHits(null);
    inputRef.current?.focus();
  };

  const onPaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    for (const item of [...(event.clipboardData?.items ?? [])]) {
      if (item.type.startsWith("image/")) {
        event.preventDefault();
        const file = item.getAsFile();
        if (file) addFile(file);
      }
    }
  };

  return (
    <div className="chat-inputbar">
      <div className="ac-box" style={{ display: acHits?.length ? "block" : "none" }}>
        {acHits?.map((command) => (
          <div
            key={command.name}
            className="ac-item"
            title={command.help}
            onMouseDown={(event) => { event.preventDefault(); pickCommand(command); }}
          >
            <div className="ac-name">{`${command.name} ${command.args ?? ""}`.trim()}</div>
            {command.help ? <div className="ac-desc">{command.help}</div> : null}
          </div>
        ))}
      </div>
      <div className="chat-composer">
        {files.length ? (
          <div className="img-preview">
            {files.map((file, index) => (
              <div
                key={`${file.name}-${index}`}
                className={`img-thumb${file.media_type.startsWith("image/") ? "" : " file"}`}
                style={file.media_type.startsWith("image/") ? { backgroundImage: `url(${file.preview})` } : undefined}
                title={`${file.name} — 点击移除`}
                onClick={() => setFiles((current) => current.filter((_, idx) => idx !== index))}
              >
                {file.media_type.startsWith("image/") ? null : (
                  <>
                    <span className="img-file-ico">📄</span>
                    <span className="img-file-name">{file.name}</span>
                  </>
                )}
              </div>
            ))}
          </div>
        ) : null}
        <textarea
          ref={inputRef}
          className={"chat-input" + (busy ? " busy" : "")}
          rows={1}
          aria-label={ariaLabel}
          placeholder={busy
            ? "生成中… 可继续输入，发送后自动加入队列"
            : placeholder}
          value={input}
          onChange={(event) => {
            const value = event.target.value;
            setInput(value);
            showAutocomplete(value);
            const element = event.currentTarget;
            element.style.height = "auto";
            element.style.height = `${Math.min(element.scrollHeight, 380)}px`;
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") { setAcHits(null); return; }
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); }
          }}
          onPaste={onPaste}
        />
        <div className="chat-composer-actions">
          <input
            ref={fileInputRef}
            type="file"
            accept={ACCEPTED_FILES}
            multiple
            hidden
            onChange={(event) => {
              if (event.target.files) for (const file of Array.from(event.target.files)) addFile(file);
              event.target.value = "";
            }}
          />
          <button className="chat-attach" type="button" title="添加图片/文件（或 Ctrl+V 粘贴图片）" onClick={() => fileInputRef.current?.click()}>
            <span className="chat-attach-ico">＋</span>
          </button>
          <span className="chat-keyhint">Enter 发送 · Shift+Enter 换行</span>
          {contextSlot}
          {steeringAvailable && onSteering ? (
            <button
              className="chat-steering-btn"
              type="button"
              title="把队列中的下一条消息插入当前 Turn（Steering）"
              aria-label="插入提示"
              onClick={() => void onSteering()}
            >
              插入提示
            </button>
          ) : null}
          <button
            className={`chat-send${busy ? " busy" : ""}`}
            type="button"
            title={busy ? "停止运行" : "发送（Enter）"}
            disabled={busy && !onStop}
            onClick={() => busy && onStop ? void onStop() : void submit()}
          >
            <span className="chat-send-ico">{busy ? "⏹" : "➤"}</span>
          </button>
        </div>
      </div>
    </div>
  );
}
