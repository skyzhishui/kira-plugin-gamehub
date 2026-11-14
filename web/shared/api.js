/* 游戏中心 · 共享 API 层：宿主 token / 响应信封 / 对局 WS 连接管理。
 *
 * KiraAI 宿主约定：
 * - 宿主口令 localStorage["jwt_token"]（WebUI 登录后写入）→ Authorization: Bearer
 * - REST 基址 /api/plugin/kira-plugin-gamehub，信封 { ok, data, error }
 * - 对局座位口令走 query ?t=，不放进 Authorization
 * - WS 不在 API 基址下：/ws/plugin/kira-plugin-gamehub/matches/{id}/ws
 *   握手只认 query（token=宿主口令，t=座位口令），浏览器 WebSocket 无法带请求头
 */

export const PLUGIN_ID = "kira-plugin-gamehub";
export const API_BASE = `/api/plugin/${PLUGIN_ID}`;
export const PAGE_BASE = `/page/plugin/${PLUGIN_ID}/hub`;
export const WS_BASE = `/ws/plugin/${PLUGIN_ID}`;

/** 宿主 JWT 的存储键（KiraAI WebUI 登录后写入）。 */
export const HOST_TOKEN_KEY = "jwt_token";

export class ApiError extends Error {
  constructor(message, status = 0, code = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    /** 稳定错误码（服务端信封的 code），供 shared/i18n.js 本地化。 */
    this.code = code || "";
  }
}

export function localToken() {
  try {
    return localStorage.getItem(HOST_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function readSessionToken(matchId) {
  try {
    return sessionStorage.getItem(`gamehub_match_${matchId}`) || "";
  } catch {
    return "";
  }
}

export function writeSessionToken(matchId, token) {
  try {
    if (matchId && token) sessionStorage.setItem(`gamehub_match_${matchId}`, String(token));
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

/** 请求插件 REST。token 缺省取宿主口令；返回信封 data，失败抛 ApiError。 */
export async function api(path, { method = "GET", body, token, timeoutMs = 12000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers = {};
    const t = token !== undefined ? token : localToken();
    if (t) headers.Authorization = `Bearer ${t}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    let payload = null;
    try {
      payload = await resp.json();
    } catch {
      /* 非 JSON 响应按失败处理 */
    }
    if (!resp.ok || !payload || payload.ok === false) {
      const msg = payload && payload.error ? payload.error : `请求失败（HTTP ${resp.status}）`;
      throw new ApiError(msg, resp.status, (payload && payload.code) || "");
    }
    return payload.data;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (err && err.name === "AbortError") throw new ApiError("请求超时，请稍后重试", 0);
    throw new ApiError("网络错误，请检查连接", 0);
  } finally {
    clearTimeout(timer);
  }
}

/** 对局 WS 地址：座位口令 ?t=，宿主口令 ?token=（宿主握手只认 query）。 */
export function matchWsUrl(matchId, seatToken) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  if (seatToken) params.set("t", seatToken);
  const hostToken = localToken();
  if (hostToken) params.set("token", hostToken);
  const q = params.toString() ? `?${params}` : "";
  return `${proto}//${location.host}${WS_BASE}/matches/${encodeURIComponent(matchId)}/ws${q}`;
}

/**
 * 对局连接：先 GET 快照，再连 WS；断线指数退避重连（1s→2s→4s…上限 15s），
 * 重连后重新拉快照；每 25s 发 ping，40s 无任何消息主动重连；
 * 页面 hidden 时不重连，visible 时立即补拉快照并重连；
 * close 4001 = 座位口令无效（降级观战），4003 = 宿主鉴权失败/插件停用。
 */
export class MatchConnection {
  constructor({ matchId, getToken, onMatch, onStatus, onError, onNotice, onAgentMessage, onLive } = {}) {
    this.matchId = String((matchId || "")).trim();
    this.getToken = typeof getToken === "function" ? getToken : () => "";
    this.onMatch = onMatch || (() => {});
    this.onStatus = onStatus || (() => {});
    this.onError = onError || (() => {});
    this.onNotice = onNotice || (() => {});
    /** 对局聊天窗口的 agent 回复帧（{type:"agent_message", sid, text[, id, ts]}）。 */
    this.onAgentMessage = onAgentMessage || (() => {});
    /** WS 每次建连成功（含断线重连）后触发：给聊天窗口补拉断线期间漏掉的帧。 */
    this.onLive = onLive || (() => {});
    this.ws = null;
    this.attempt = 0;
    this.stopped = false;
    this.hidden = false;
    this.tokenRejected = false; // 4001 后置位：后续请求不再携带座位口令
    this.pingTimer = 0;
    this.watchTimer = 0;
    this.retryTimer = 0;
    this.lastMessageAt = 0;
    this._status = "";
    this._onVis = () => this._visibility();
  }

  start() {
    this.stopped = false;
    this.hidden = document.hidden;
    document.addEventListener("visibilitychange", this._onVis);
    if (this.hidden) {
      this._setStatus("hidden");
      return;
    }
    this._bootstrap();
  }

  destroy() {
    this.stopped = true;
    document.removeEventListener("visibilitychange", this._onVis);
    this._clearTimers();
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    }
  }

  /** 拉一次快照（不动 WS）。口令无效时降级观战重试一次。 */
  async pullSnapshot() {
    const token = this.getToken();
    const q = token ? `?t=${encodeURIComponent(token)}` : "";
    try {
      const data = await api(`/matches/${encodeURIComponent(this.matchId)}${q}`);
      this.onMatch(data && data.match);
    } catch (err) {
      if (token && (err.status === 400 || err.status === 401 || err.status === 403)) {
        this._dropToken(err.message);
        return this.pullSnapshot();
      }
      throw err;
    }
  }

  // ------------------------------------------------------------内部

  async _bootstrap() {
    if (this.stopped || this.hidden) return;
    this._setStatus("loading");
    try {
      await this.pullSnapshot();
    } catch (err) {
      if (err && (err.status === 404 || err.status === 410)) {
        this._setStatus("missing");
        this.onError(err);
        return;
      }
      this._scheduleRetry();
      return;
    }
    if (this.stopped || this.hidden) return;
    this._openWs();
  }

  _openWs() {
    if (this.stopped || this.hidden) return;
    this._clearTimers();
    this._setStatus("connecting");
    let ws;
    try {
      ws = new WebSocket(matchWsUrl(this.matchId, this.tokenRejected ? "" : this.getToken()));
    } catch {
      this._scheduleRetry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.lastMessageAt = Date.now();
      this._startHeartbeat();
      this.onLive();
    };
    ws.onmessage = (ev) => this._onFrame(ev);
    ws.onerror = () => {}; // 错误随后必然触发 onclose，统一在那里处理
    ws.onclose = (ev) => this._onClose(ev);
  }

  _onFrame(ev) {
    this.lastMessageAt = Date.now();
    let frame = null;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (!frame || typeof frame !== "object") return;
    if (frame.type === "match" && frame.match) {
      this.attempt = 0;
      this._setStatus("live");
      this.onMatch(frame.match);
    } else if (frame.type === "pong") {
      this.attempt = 0;
      if (this._status !== "live") this._setStatus("live");
    } else if (frame.type === "error") {
      this.onError(new ApiError(frame.message || "服务器返回错误", 0, frame.code || ""));
    } else if (frame.type === "agent_message") {
      // 对局聊天窗口的 agent 回复：与座位气泡（match.chat）互不影响
      this.onAgentMessage(frame);
    }
    // 其余帧类型忽略（向前兼容）
  }

  _onClose(ev) {
    this._stopHeartbeat();
    this.ws = null;
    if (this.stopped || this.hidden) return;
    const code = ev ? ev.code : 0;
    if (code === 4003) {
      // KiraAI 宿主握手失败（缺 token / token 失效）时也走 4003：不再重试。
      this.stopped = true;
      this._setStatus("stopped");
      this.onNotice("连接已关闭（鉴权失败或插件已停用），请重新登录宿主 WebUI 后再试。");
      return;
    }
    if (code === 4001 && !this.tokenRejected) {
      this.tokenRejected = true;
      this.attempt = 0;
      this.onNotice("座位口令无效，已切换为观战视角。");
      this._setStatus("connecting");
      this.retryTimer = setTimeout(() => {
        this.retryTimer = 0;
        this._bootstrap();
      }, 300);
      return;
    }
    this._scheduleRetry();
  }

  _scheduleRetry() {
    if (this.stopped || this.hidden) return;
    const delay = Math.min(1000 * Math.pow(2, Math.min(this.attempt, 4)), 15000);
    this.attempt += 1;
    this._setStatus("retrying", { attempt: this.attempt });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = 0;
      this._bootstrap();
    }, delay);
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    this.pingTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(JSON.stringify({ type: "ping" }));
        } catch {
          /* ignore */
        }
      }
    }, 25000);
    this.watchTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN && Date.now() - this.lastMessageAt > 40000) {
        try {
          this.ws.close(); // 触发 onclose → 退避重连
        } catch {
          /* ignore */
        }
      }
    }, 5000);
  }

  _stopHeartbeat() {
    if (this.pingTimer) clearInterval(this.pingTimer);
    if (this.watchTimer) clearInterval(this.watchTimer);
    this.pingTimer = 0;
    this.watchTimer = 0;
  }

  _clearTimers() {
    this._stopHeartbeat();
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = 0;
    }
  }

  _visibility() {
    if (document.hidden) {
      this.hidden = true;
      this._clearTimers();
      if (this.ws) {
        const ws = this.ws;
        this.ws = null;
        ws.onclose = null;
        ws.onmessage = null;
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      }
      this._setStatus("hidden");
    } else {
      this.hidden = false;
      if (this.stopped) return;
      this.attempt = 0;
      this._bootstrap(); // visible：立即补拉快照 + 重连
    }
  }

  _dropToken(msg) {
    if (this.tokenRejected) return;
    this.tokenRejected = true;
    this.onNotice(msg || "座位口令无效，已切换为观战视角。");
  }

  _setStatus(s, info) {
    this._status = s;
    this.onStatus(s, info);
  }
}
