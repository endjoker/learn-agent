import { useCallback, useEffect, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import type { SkillDetail, SkillInfo, SkillsMeta, SkillsResponse } from "@/api/types";
import { Markdown } from "@/components/Markdown";
import { Modal } from "@/components/Modal";

export function SkillsPage({ client = defaultApi }: { client?: ApiClient }) {
  const [meta, setMeta] = useState("");
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [detail, setDetail] = useState<SkillDetail | null>(null);

  const loadMeta = useCallback(async () => {
    try {
      const m = await client.get<SkillsMeta>("/api/skills/meta", { silent: true });
      setMeta(`技能目录: ${m.skills_dir ?? ""}${m.exists ? "" : "（不存在）"}${m.platform_note ? `  ⚠️ ${m.platform_note}` : ""}`);
    } catch { setMeta(""); }
  }, [client]);

  const refresh = useCallback(async () => {
    try {
      const data = await client.get<SkillsResponse>("/api/skills", { silent: true });
      setSkills(data.skills ?? []);
    } catch { /* silent */ }
  }, [client]);

  useEffect(() => {
    void loadMeta();
    void refresh();
  }, [loadMeta, refresh]);

  const showInstruction = async (name: string) => {
    try {
      const data = await client.get<SkillDetail>(`/api/skills/${encodeURIComponent(name)}`);
      setDetail(data);
    } catch { /* silent */ }
  };

  return (
    <section className="page" aria-label="Skills 页面">
      <h1>🧩 Skills</h1>
      <div className="skills-meta dim">{meta}</div>
      <div className="skills-grid">
        {skills.length === 0 ? <div className="placeholder">暂无技能（SKILLS 目录为空）</div> : null}
        {skills.map((s) => (
          <div key={s.name} className="skill-card">
            <div className="skill-head"><b>{s.name}</b><span className="dim">{`v${s.version ?? 1}`}</span></div>
            <div className="skill-desc">{s.description ?? ""}</div>
            {(s.tags ?? []).length ? (
              <div className="skill-tags">{s.tags!.map((tag) => <span key={tag} className="badge dim">{tag}</span>)}</div>
            ) : null}
            <div className="dim" style={{ marginTop: 6 }}>{`${s.instruction_chars ?? 0} 字符指令`}</div>
            <button type="button" className="btn" onClick={() => void showInstruction(s.name)}>查看指令</button>
          </div>
        ))}
      </div>
      {detail ? (
        <Modal
          title={`🧩 ${detail.name} — instruction.md`}
          actions={<button type="button" className="btn" onClick={() => setDetail(null)}>关闭</button>}
          wide
        >
          <Markdown text={detail.instruction ?? ""} className="md skill-instr" />
        </Modal>
      ) : null}
    </section>
  );
}
