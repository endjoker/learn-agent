import { useMemo, useState } from "react";

export interface SearchSelectorItem {
  id: string;
  name: string;
  description?: string;
  risk?: string;
  available?: boolean;
  unavailable_reason?: string;
}

const RISK_LABELS: Record<string, string> = {
  low: "低风险（只读/查询）",
  medium: "中风险（写入/网络）",
  high: "高风险（执行命令/代码）",
};

export interface SearchSelectorProps {
  items: SearchSelectorItem[];
  selected: Set<string>;
  onToggle: (id: string, checked: boolean) => void;
  placeholder?: string;
}

/** Searchable checkbox grid mirroring the legacy HA.SearchSelector. */
export function SearchSelector({ items, selected, onToggle, placeholder = "搜索…" }: SearchSelectorProps) {
  const [query, setQuery] = useState("");
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => (item.name || item.id).toLowerCase().includes(q));
  }, [items, query]);

  return (
    <div className="ws-selector">
      <input type="text" value={query} placeholder={placeholder} onChange={(event) => setQuery(event.target.value)} />
      <div className="ws-check-grid">
        {shown.length === 0 ? <div className="ws-empty">无匹配项</div> : null}
        {shown.map((item) => {
          const id = item.id || item.name;
          const checked = selected.has(id);
          const disabled = item.available === false;
          const riskTag = item.risk
            ? <span className={`badge ${item.risk}`}>{RISK_LABELS[item.risk] ?? item.risk}</span>
            : null;
          return (
            <label key={id} className={`ws-check${disabled ? " dim" : ""}`} title={disabled ? item.unavailable_reason ?? "当前运行环境不可用" : undefined}>
              <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onToggle(id, event.target.checked)} />
              <span className="ws-check-text">
                <span className="ws-check-name">{item.name || id}</span>
                {item.description ? <span className="ws-check-desc">{item.description}</span> : null}
              </span>
              {riskTag}
            </label>
          );
        })}
      </div>
    </div>
  );
}
