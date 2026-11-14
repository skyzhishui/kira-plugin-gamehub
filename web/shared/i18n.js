/* i18n.js — 游戏中心错误文案本地化（浏览器原生 ESM，零依赖）。
 *
 * 服务端错误信封带稳定错误码：{ ok:false, data:null, error:"<中文>", code:"<key>" }。
 * 前端按宿主语言把 code 映射成短句；code 缺失/未知时退回服务端中文文案。
 * 范围仅限错误文案：页面正文仍为中文。
 */

/** 错误码 → 中英文文案（zh 与服务端中文文案一致）。 */
export const ERROR_TEXT = {
  bad_action: { zh: "操作格式不正确", en: "Malformed action." },
  bad_kind: { zh: "没有这个游戏", en: "Unknown game." },
  bad_mode: { zh: "不支持的模式或模式切换", en: "Unsupported mode or mode switch." },
  bad_name: { zh: "昵称不合法", en: "Invalid display name." },
  bad_opponent: { zh: "对手不合法", en: "Invalid opponent." },
  bad_request: { zh: "请求不合法", en: "Invalid request." },
  bad_seat: { zh: "该座位不可操作", en: "This seat cannot be controlled." },
  bad_token: { zh: "座位口令无效", en: "Invalid seat token." },
  game_over: { zh: "对局已结束", en: "This match has ended." },
  not_found: { zh: "对局不存在", en: "Match not found." },
  not_ready: { zh: "对局服务暂不可用", en: "Game service is not ready." },
  actor_busy: { zh: "你已有一局未结束的对局", en: "You already have an unfinished match." },
  match_full: { zh: "对局席位已满", en: "This match is full." },
  stale_version: { zh: "棋局版本已更新，请同步后再操作", en: "The board changed; refresh and try again." },
  internal: { zh: "插件内部错误", en: "Internal plugin error." },
};

const GENERIC = { zh: "出现了一个小问题，请重试", en: "Something went wrong. Please retry." };

/** 宿主语言：localStorage["language"] === "en" 时为 en，否则 zh。 */
export function uiLanguage() {
  try {
    return localStorage.getItem("language") === "en" ? "en" : "zh";
  } catch {
    return "zh";
  }
}

/**
 * 错误文案：优先按 err.code 本地化，其次 err.message，再其次 fallback，
 * 最后是通用文案。返回纯文本（调用方用 textContent 渲染）。
 */
export function errorText(err, fallback = "") {
  const lang = uiLanguage();
  const code = err && typeof err.code === "string" ? err.code : "";
  if (code && ERROR_TEXT[code]) return ERROR_TEXT[code][lang] || ERROR_TEXT[code].zh;
  const message = err && typeof err.message === "string" ? err.message.trim() : "";
  if (message) return message;
  const fb = typeof fallback === "string" ? fallback.trim() : "";
  if (fb) return fb;
  return GENERIC[lang];
}
