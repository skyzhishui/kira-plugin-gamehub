/* games/registry.js — 游戏模块注册表：按 kind 动态加载页面模块。
 *
 * 模块契约（§5.7）：default export 含 mount(host, { onPlace, meta }) → { update(match, opts), destroy() }。
 *
 * 目录结构支持两种布局（新增棋种建议目录式，专属代码/素材放各自目录）：
 * - 目录模块：web/games/<kind>/index.js（如 reversi，显式登记在 DIR_MODULES）
 * - 平铺模块：web/games/<kind>.js（如 gomoku，保持原位，不做无谓迁移）
 * 未登记的 kind 先试平铺、再试目录；登记的 kind 先试目录、再试平铺。任一布局
 * 导入失败会可靠回退另一种，模块缺失/语法错误一律返回 null（页面降级提示）。
 *
 * kind 白名单校验防路径注入；导入结果按 kind 缓存（同一类型只解析一次）。
 */

const cache = new Map();
const SAFE_KIND = /^[a-z][a-z0-9_]*$/;

/** 目录式游戏模块登记表：新增目录模块在此加一行（同时放 games/<kind>/index.js）。 */
export const DIR_MODULES = new Set(["reversi", "doudizhu"]);

function normalizeKind(kind) {
  return String(kind == null ? "" : kind).trim().toLowerCase();
}

/** 候选模块地址（按布局优先级；kind 非法返回空数组）。 */
export function moduleUrls(kind) {
  const k = normalizeKind(kind);
  if (!SAFE_KIND.test(k)) return [];
  const flat = new URL(`./${k}.js`, import.meta.url).href;
  const dir = new URL(`./${k}/index.js`, import.meta.url).href;
  return DIR_MODULES.has(k) ? [dir, flat] : [flat, dir];
}

export function moduleUrl(kind) {
  return moduleUrls(kind)[0] || "";
}

/** 目录式模块的专属素材地址（页面相对路径；非目录模块返回空串）。 */
export function gameAssetPath(kind, file) {
  const k = normalizeKind(kind);
  if (!SAFE_KIND.test(k) || !DIR_MODULES.has(k)) return "";
  const name = String(file == null ? "" : file).replace(/^\/+/, "");
  if (!name || name.includes("..") || name.includes("/")) return "";
  return `./games/${k}/assets/${name}`;
}

/** 大厅封面候选：游戏目录内专属素材优先（png → svg），回退公共 assets 旧约定。
 * 全部失败时由调用方移除图片（不塌卡、不阻塞功能）。 */
export function cardCoverCandidates(kind) {
  const k = normalizeKind(kind);
  if (!SAFE_KIND.test(k)) return [];
  const out = [];
  if (DIR_MODULES.has(k)) {
    out.push(`./games/${k}/assets/card-${k}.png`, `./games/${k}/assets/card-${k}.svg`);
  }
  out.push(`./assets/card-${k}.png`);
  return out;
}

async function importFirst(urls) {
  for (const url of urls) {
    try {
      const mod = await import(url);
      if (mod && mod.default) return mod;
    } catch {
      /* 该布局缺失/语法错误：继续尝试下一种 */
    }
  }
  return null;
}

export function loadGameModule(kind) {
  const k = normalizeKind(kind);
  const urls = moduleUrls(k);
  if (!urls.length) return Promise.resolve(null);
  if (!cache.has(k)) {
    cache.set(k, importFirst(urls));
  }
  return cache.get(k);
}