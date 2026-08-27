#!/usr/bin/env node
// 将 frontend/dist 构建产物同步到网关静态目录 gateway/webui/static/
// 用法: npm run sync:static （在 gateway/webui/frontend/ 下执行）
// 构建产物（node_modules / dist / static/assets）不入 git，克隆源码后必须构建并同步，
// 网关 /ui/ 才能正常显示页面。
//
// 同步策略（防版本漂移）：
// 1) 先整体清空 static/assets 再复制——避免 vite 入口命名变化后旧 hash 产物永久残留；
// 2) 不复制 .map 文件（生产构建已关闭 sourcemap，此处兜底防止旧 map 残留暴露源码）；
// 3) 结束前校验 static/index.html 引用的每个 assets/<file> 都存在于磁盘，
//    否则非零退出——防止"只跑了 build 忘了 sync"或半同步状态静默上线旧 UI。
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, copyFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const distDir = join(frontendDir, "dist");
const staticDir = resolve(frontendDir, "..", "static");

if (!existsSync(join(distDir, "index.html"))) {
  console.error("❌ dist/index.html 不存在——请先运行 npm run build");
  process.exit(1);
}

// 0) 全新克隆时 static/ 可能不存在（目录不打包）——确保目标根存在
mkdirSync(staticDir, { recursive: true });

// 1) 根文件（入口 + 样式）
for (const name of ["index.html", "style.css", "workspace.css"]) {
  const src = join(distDir, name);
  if (existsSync(src)) copyFileSync(src, join(staticDir, name));
}

// 2) assets：整体清空再全量复制（跳过 .map）
const assetsDist = join(distDir, "assets");
const assetsStatic = join(staticDir, "assets");
mkdirSync(assetsStatic, { recursive: true });
for (const f of readdirSync(assetsStatic)) {
  rmSync(join(assetsStatic, f), { force: true, recursive: true });
}
let copied = 0;
if (existsSync(assetsDist)) {
  for (const f of readdirSync(assetsDist)) {
    if (f.endsWith(".map")) continue; // 不对外发布 sourcemap
    copyFileSync(join(assetsDist, f), join(assetsStatic, f));
    copied += 1;
  }
}

// 3) 一致性校验：index.html 引用的 assets 必须都在磁盘上
const html = readFileSync(join(staticDir, "index.html"), "utf-8");
const refs = [...html.matchAll(/assets\/([A-Za-z0-9._-]+)/g)].map((m) => m[1]);
const missing = refs.filter((f) => !existsSync(join(assetsStatic, f)));
if (missing.length > 0) {
  console.error("❌ 一致性校验失败：index.html 引用了不存在的产物:", missing);
  process.exit(2);
}

console.log(`✅ 已同步 ${copied} 个 asset →`, staticDir);
console.log("   刷新浏览器（Ctrl+Shift+R）即可看到新 UI；如网关在运行中无需重启。");
