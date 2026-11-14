/* media.js — 对局页媒体共享层：消息媒体解析 + 认证图片装载 + TTS 语音自动播放。
 *
 * KiraAI 宿主差异（相对 nori 原版）：
 * - 媒体端点换成 KiraAI 的两条路由，令牌仍然只进请求头（<img>/<audio> 无法带
 *   Authorization，统一 fetch(Bearer) → blob object URL）：
 *   · 归档消息媒体：/api/messages/{message_id}/media/{element_path}
 *   · 网页聊天媒体：/api/webchat/messages/{message_id}/media/{element_path}
 *   element_path 是消息 chain 的下标路径（如 "0" / "2.1"），由宿主校验格式。
 * - 消息载荷两种形态都认：
 *   · KiraAI 归档形态 msg.chain = [{type:"image"|"sticker"|"record"|…, file_type:"archive"}]
 *   · nori 形态 msg.segments[i] = {type, hash, local_url}（旧载荷/测试 fixture）
 * - 自动播放（仅 TTS 语音）语义不变：只播「新」消息、同 key 去重、串行、
 *   被浏览器拦截时不自动重试（等真实手势 unlock）、dispose 释放全部 blob URL。
 */

/* ==================== 纯协议解析（无 DOM，Node 可测） ==================== */

/** 用户侧媒体被多模态替换后的描述文本段前缀 */
const MEDIA_TEXT_PREFIX_RE = /^\[(图片描述|表情包|屏幕感知)\]/;

/** content 里的媒体 markdown：![描述](图片) / [描述](语音) */
const MEDIA_MD_RE = /(!?)\[([^\]]*)\]\(([^)\s]+)\)/g;

/** 占位描述里可剥离的标记前缀（[表情包]描述 / [表情包:情绪]） */
const MARK_PREFIX_RE = /^\[(?:图片描述|表情包|表情|图片|语音|屏幕感知)(?:[:：][^\]]*)?\]/;

/** 宿主校验的 element_path 形态：最多四层下标。 */
const ELEMENT_PATH_RE = /^[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}$/;

/** chain 元素 type → 渲染种类（KiraAI ElementType 取值）。 */
const CHAIN_IMAGE_TYPES = new Set(["image", "sticker", "screen", "emoji"]);
const CHAIN_AUDIO_TYPES = new Set(["record", "voice", "audio"]);
const CHAIN_FILE_TYPES = new Set(["file", "video"]);

function defaultBase() {
  return typeof location !== "undefined" && location.href ? location.href : "http://localhost/";
}

/** 同源 URL 判定（外链/data:/blob: 原样，绝不改写或附带令牌）。 */
export function isSameOriginUrl(url, base) {
  if (!url || /^(data|blob):/i.test(url)) return false;
  const b = base || defaultBase();
  try {
    return new URL(url, b).origin === new URL(b).origin;
  } catch {
    return false;
  }
}

/** element_path 归一：只允许下标路径，非法返回空串（调用方据此不渲染）。 */
export function normalizeElementPath(path) {
  const p = String(path == null ? "" : path).trim();
  return ELEMENT_PATH_RE.test(p) ? p : "";
}

/** 归档消息媒体（宿主 /api/messages 路由）。 */
export function archivedMediaUrl(messageId, elementPath) {
  const id = String(messageId == null ? "" : messageId).trim();
  const path = normalizeElementPath(elementPath);
  if (!id || !path) return "";
  return `/api/messages/${encodeURIComponent(id)}/media/${path}`;
}

/** 网页聊天媒体（宿主 /api/webchat 路由）。 */
export function webchatMediaUrl(messageId, elementPath) {
  const id = String(messageId == null ? "" : messageId).trim();
  const path = normalizeElementPath(elementPath);
  if (!id || !path) return "";
  return `/api/webchat/messages/${encodeURIComponent(id)}/media/${path}`;
}

/** 描述文本段（前缀 + hash）→ 渲染原图；描述保留为悬停提示，不再显示文本占位。 */
export function isMediaTextSeg(seg) {
  return !!(seg && seg.type === "text" && seg.hash && MEDIA_TEXT_PREFIX_RE.test(String(seg.text || "")));
}

/** 段的渲染种类："image" | "voice" | null（非媒体段）。 */
export function segmentKind(seg) {
  const t = String((seg && seg.type) || "");
  if (t === "voice" || t === "record") return "voice";
  if (t === "emoji" || t === "image" || t === "screen" || t === "sticker" || isMediaTextSeg(seg)) return "image";
  return null;
}

/**
 * 段的可渲染 src：local_url → preview（data URL）→ 宿主媒体端点。
 * local_media=false（后端确认未缓存）返回 ""，调用方渲染占位、不发请求。
 * ctx = { messageId, scope:"archive"|"webchat" }：缺 messageId 时无法拼端点 → ""。
 */
export function resolveSegmentSrc(seg, ctx = {}) {
  if (!seg) return "";
  if (seg.local_media === false) return "";
  if (seg.local_url) return String(seg.local_url);
  if (seg.preview) return String(seg.preview);
  const messageId = ctx && ctx.messageId != null ? ctx.messageId : seg.message_id;
  const build = ctx && ctx.scope === "webchat" ? webchatMediaUrl : archivedMediaUrl;
  return build(messageId, seg.element_path != null ? seg.element_path : seg.hash);
}

/** 剥掉系统标记前缀，得到可读标题（占位/芯片文案用）。 */
export function cleanMediaTitle(text) {
  return String(text || "").replace(MARK_PREFIX_RE, "").trim();
}

/**
 * content markdown → parts；无媒体 markdown 时返回 null（调用方按纯文本渲染）。
 * 图片/语音 URL 原样保留（同源走 Bearer blob，外链交给浏览器直连）。
 */
export function parseContentMarkdown(content) {
  const text = String(content || "");
  const re = new RegExp(MEDIA_MD_RE.source, "g");
  const parts = [];
  let last = 0;
  let m;
  let hit = false;
  while ((m = re.exec(text)) !== null) {
    hit = true;
    if (m.index > last) parts.push({ kind: "text", text: text.slice(last, m.index) });
    if (m[1] === "!") {
      parts.push({ kind: "image", src: m[3], title: m[2] || "" });
    } else {
      parts.push({ kind: "audio", src: m[3], title: m[2] || "语音" });
    }
    last = re.lastIndex;
  }
  if (!hit) return null;
  if (last < text.length) parts.push({ kind: "text", text: text.slice(last) });
  return parts;
}

/** KiraAI chain 元素 → 可渲染 part（相对下标路径用于媒体端点）。 */
function chainPart(el, path, ctx) {
  if (!el || typeof el !== "object") return null;
  const type = String(el.type || "");
  const title = String(el.caption || el.description || el.transcript || el.name || "");
  // file_type=unavailable 是宿主确认媒体不可用（已归档失败）→ 不再等重拉；
  // 其余（archive 但缺 id 等）按「注解未就绪」处理，水位先不越过该消息。
  const deferred = el.file_type !== "unavailable";
  if (CHAIN_IMAGE_TYPES.has(type)) {
    const src = el.file_type === "archive" ? archivedMediaUrl(ctx.messageId, path) : "";
    return src ? { kind: "image", src, title } : { kind: "placeholder", voice: false, title, deferred };
  }
  if (CHAIN_AUDIO_TYPES.has(type)) {
    const src = el.file_type === "archive" ? archivedMediaUrl(ctx.messageId, path) : "";
    return src ? { kind: "audio", src, title } : { kind: "placeholder", voice: true, title, deferred };
  }
  if (CHAIN_FILE_TYPES.has(type)) {
    return { kind: "placeholder", voice: false, title, deferred: false };
  }
  if (type === "text" && el.text) return { kind: "text", text: String(el.text) };
  return null;
}

/**
 * 消息 → 可渲染 parts（对局页气泡/占位共用）。
 * 返回 {parts, fromSegments}；parts 元素：
 *   {kind:"text", text} | {kind:"image", src, title} |
 *   {kind:"audio", src, title} |
 *   {kind:"placeholder", voice, title, deferred}
 * deferred=true 表示媒体段存在但注解未就绪（宿主可能稍后才归档）→ 自动播放
 * 水位先不越过该消息；local_media=false 是后端确认未缓存 → deferred=false。
 * chain（KiraAI 归档形态）优先；否则读 segments；都没有则回退 content markdown。
 */
export function extractMessageParts(msg, ctx = {}) {
  const messageId = ctx && ctx.messageId != null ? ctx.messageId : msg && msg.id;
  const scope = (ctx && ctx.scope) || "archive";
  const chain = Array.isArray(msg && msg.chain) ? msg.chain : [];
  if (chain.length) {
    const parts = [];
    chain.forEach((el, i) => {
      const part = chainPart(el, String(i), { messageId, scope });
      if (part) parts.push(part);
      else if (el && el.type === "reply" && Array.isArray(el.chain)) {
        // 引用消息：只在文本上标注，不递归展开媒体
        parts.push({ kind: "text", text: `[引用] ${String(el.message_content || "").trim()}`.trim() });
      }
    });
    if (parts.length) return { parts, fromSegments: true };
  }

  const segs = Array.isArray(msg && msg.segments) ? msg.segments.filter((s) => s && s.type) : [];
  if (segs.length) {
    const parts = [];
    for (const s of segs) {
      const kind = segmentKind(s);
      if (kind) {
        const src = resolveSegmentSrc(s, { messageId, scope });
        const title = String(s.text || "");
        if (!src) {
          parts.push({
            kind: "placeholder",
            voice: kind === "voice",
            title,
            deferred: s.local_media !== false,
          });
        } else if (kind === "voice") {
          parts.push({ kind: "audio", src, title });
        } else {
          parts.push({ kind: "image", src, title });
        }
      } else if (s.type === "text" && s.text) {
        parts.push({ kind: "text", text: String(s.text) });
      }
    }
    if (parts.length) return { parts, fromSegments: true };
  }

  const content = String((msg && msg.content) || "");
  const md = parseContentMarkdown(content);
  if (md) return { parts: md, fromSegments: false };
  return { parts: content ? [{ kind: "text", text: content }] : [], fromSegments: false };
}

/**
 * 自动播放选择器（水位线）。
 * - firstLoad=true（初次拉历史 / 冷启动）：只推水位，绝不返回 jobs（历史不重播）；
 * - 之后每次重拉：仅人设（role=assistant / is_self）「新」消息的语音段生成 jobs；
 * - 用户消息永不自动播；无 DB id 的乐观消息永不自动播；
 * - 消息含「注解未就绪」的语音占位（deferred）时水位先不越过它。
 * 返回 {jobs:[{key,url,title}], nextWatermark}；key = `${id}:${partIndex}`。
 */
export function selectAutoPlayVoices(items, watermark, { firstLoad = false } = {}) {
  const base = Number(watermark) || 0;
  let next = base;
  let holdBelowId = Infinity; // 含未就绪语音段的最小新消息 id：水位停在它之前
  const jobs = [];
  for (const m of items) {
    const id = Number(m && m.id);
    const nori = !!(m && (m.role === "assistant" || m.is_self === true));
    if (!Number.isFinite(id) || !nori) continue;
    if (id > next) next = id;
    if (firstLoad || id <= base) continue;
    const { parts } = extractMessageParts(m, { messageId: m.id });
    let deferredVoice = false;
    parts.forEach((p, i) => {
      if (p.kind === "audio") {
        jobs.push({ key: `${id}:${i}`, url: p.src, title: p.title || "语音" });
      } else if (p.kind === "placeholder" && p.voice && p.deferred) {
        deferredVoice = true;
      }
    });
    if (deferredVoice && id < holdBelowId) holdBelowId = id;
  }
  if (Number.isFinite(holdBelowId)) next = Math.max(base, Math.min(next, holdBelowId - 1));
  return { jobs, nextWatermark: next };
}

/* ==================== 认证媒体加载（Bearer → blob URL） ==================== */

/**
 * 同源媒体 fetch（Bearer）→ object URL；并发合并、LRU 缓存、dispose 全量 revoke。
 * 外链 / data: / blob: 原样返回；令牌只进请求头，绝不进 URL。
 * 全部依赖可注入（fetchImpl / createObjectUrl / getToken），Node 可测。
 */
export function createMediaLoader({
  getToken = () => "",
  fetchImpl = null,
  createObjectUrl = null,
  cacheMax = 40,
} = {}) {
  const doFetch = fetchImpl || (typeof fetch === "function" ? fetch : null);
  const makeObjectUrl = createObjectUrl || ((blob) => URL.createObjectURL(blob));
  const cache = new Map(); // url → objectUrl（Map 顺序即 LRU）
  const inflight = new Map();
  const created = new Set();
  let disposed = false;

  async function objectUrl(url) {
    if (disposed) throw new Error("媒体加载器已释放");
    const u = String(url || "");
    if (!u) throw new Error("缺少媒体地址");
    if (!isSameOriginUrl(u)) return u; // data:/blob:/外链原样
    const hit = cache.get(u);
    if (hit) return hit;
    const pending = inflight.get(u);
    if (pending) return pending;
    if (!doFetch) throw new Error("当前环境不可用 fetch");
    const task = (async () => {
      const headers = {};
      const t = getToken();
      if (t) headers.Authorization = `Bearer ${t}`;
      let resp;
      try {
        resp = await doFetch(new URL(u, defaultBase()).href, {
          headers,
          credentials: "same-origin",
        });
      } catch {
        throw new Error("媒体加载失败：网络错误");
      }
      if (!resp.ok) throw new Error(`媒体加载失败（HTTP ${resp.status}）`);
      const blob = await resp.blob();
      // revokeAll 后迟到的响应绝不回填缓存/创建 object URL
      if (disposed) throw new Error("媒体加载器已释放");
      const obj = makeObjectUrl(blob);
      if (disposed) {
        try {
          URL.revokeObjectURL(obj);
        } catch {
          /* ignore */
        }
        throw new Error("媒体加载器已释放");
      }
      created.add(obj);
      if (cache.size >= cacheMax) {
        const oldest = cache.keys().next().value;
        cache.delete(oldest); // 仍被 DOM 引用的 blob 由浏览器按引用计数保活
      }
      cache.set(u, obj);
      return obj;
    })();
    inflight.set(u, task);
    try {
      return await task;
    } finally {
      if (inflight.get(u) === task) inflight.delete(u);
    }
  }

  function revokeAll() {
    created.forEach((obj) => {
      try {
        URL.revokeObjectURL(obj);
      } catch {
        /* ignore */
      }
    });
    created.clear();
    cache.clear();
    inflight.clear();
    disposed = true;
  }

  return { objectUrl, revokeAll, isSameOriginUrl };
}

/* ==================== TTS 播放队列（串行 / 去重 / 自动播放拦截） ==================== */

function isBlockedError(err) {
  const name = err && err.name;
  return name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError";
}

/**
 * 单声道串行播放队列。makeAudio 注入（生产 new Audio()；测试用 fake），
 * onState({state,key}) 驱动 UI：speaker 动效只认 state==="playing"。
 * 状态机：idle → loading → playing ⇄ paused → idle（ended）/ error / blocked。
 * - enqueue(job)：key 去重（seen 集合，同一消息永不重播）；超上限丢最旧；
 * - toggle(job)：芯片点击 —— 播放中→暂停、暂停中→续播、否则插队手动播；
 * - 自动播放被拒（NotAllowedError）：不自动重试、不继续出队（不刷权限），
 *   转入 blocked；unlock() 必须由真实用户手势处理器调用；
 * - dispose()：暂停当前、清空队列，离页/清理时调用。
 */
export function createTtsQueue({
  loadSrc = (url) => Promise.resolve(url),
  makeAudio = null,
  onState = () => {},
  maxJobs = 8,
  maxAttempts = 3,
} = {}) {
  const emit = typeof onState === "function" ? onState : () => {};
  const factory =
    makeAudio ||
    (typeof Audio === "function"
      ? (src) => {
          const a = new Audio();
          a.preload = "auto";
          a.src = src;
          return a;
        }
      : null);
  const queued = [];
  const seen = new Set(); // 进过队列的 key：同消息（聊天窗+气泡两份渲染）不重复播
  let current = null; // {job, audio, finishing}
  let blockedJob = null; // 被自动播放策略拦截、等待手势解锁的 job
  let state = "idle";
  let stateKey = null;
  let disposed = false;
  let pumping = false;

  function setState(next, key = null) {
    state = next;
    stateKey = key;
    emit({ state, key });
  }

  function detach(audio) {
    if (!audio || !audio.__noriTtsH) return;
    const h = audio.__noriTtsH;
    ["playing", "pause", "ended", "error"].forEach((t) => {
      try {
        audio.removeEventListener(t, h[t]);
      } catch {
        /* ignore */
      }
    });
    audio.__noriTtsH = null;
  }

  function cleanupAudio(audio) {
    try {
      audio.pause();
    } catch {
      /* ignore */
    }
    try {
      if (typeof audio.removeAttribute === "function") audio.removeAttribute("src");
      if (typeof audio.load === "function") audio.load(); // 释放解码/网络管线
    } catch {
      /* ignore */
    }
  }

  function attach(audio, job) {
    const h = {
      playing: () => {
        if (current && current.job.key === job.key && !current.finishing) {
          setState("playing", job.key); // 唯一「真的在响」信号：speaker 动效只认它
        }
      },
      pause: () => {
        if (current && current.job.key === job.key && !current.finishing) {
          if (audio && audio.paused === false) return; // 切源等噪音暂停
          setState("paused", job.key);
        }
      },
      ended: () => {
        if (!current || current.job.key !== job.key || current.finishing) return;
        current.finishing = true;
        detach(audio);
        cleanupAudio(audio);
        current = null;
        setState("idle");
        pump();
      },
      error: () => {
        if (!current || current.job.key !== job.key || current.finishing) return;
        current.finishing = true;
        detach(audio);
        cleanupAudio(audio);
        current = null;
        setState("error", job.key);
        pump(); // 出错跳下一条：同一 key 已在 seen，绝不无限重试
      },
    };
    audio.__noriTtsH = h;
    ["playing", "pause", "ended", "error"].forEach((t) => {
      audio.addEventListener(t, h[t]);
    });
  }

  function dropCurrent(nextState, key) {
    if (!current) return;
    current.finishing = true;
    const { audio } = current;
    detach(audio);
    cleanupAudio(audio);
    current = null;
    setState(nextState, key);
  }

  async function startJob(job, manual) {
    let src;
    try {
      src = await loadSrc(job.url);
    } catch {
      return finishError(job);
    }
    if (disposed) return;
    let audio;
    try {
      if (!factory) throw new Error("无音频工厂");
      audio = factory(src, job);
      if (!audio || typeof audio.play !== "function") throw new Error("音频实例无效");
    } catch {
      return finishError(job);
    }
    current = { job, audio, finishing: false };
    attach(audio, job);
    setState("loading", job.key);
    try {
      await audio.play();
      // play() resolve ≠ 出声：playing 事件到达才置 playing（动效仅实际播放出现）
    } catch (err) {
      if (isBlockedError(err)) {
        job.attempts = (job.attempts || 0) + 1;
        const giveUp = job.attempts >= maxAttempts;
        detach(audio);
        cleanupAudio(audio);
        current = null;
        if (giveUp) {
          setState("error", job.key); // 多次手势仍被拒：放弃自动播（不声称已播放）
          pump();
          return;
        }
        blockedJob = job;
        setState("blocked", job.key); // 等真实手势 unlock()，不自动重试
        return;
      }
      return failCurrent(job, audio);
    }
  }

  function finishError(job) {
    // 拉取失败 / 实例失败（尚未进入 current）：置 error 后继续队列
    setState("error", job.key);
    if (!disposed && !current && !blockedJob) pump();
  }

  /** play() 抛出非拦截类错误（如格式不支持）：清场当前任务再继续队列。 */
  function failCurrent(job, audio) {
    if (current && current.job.key === job.key) {
      current.finishing = true;
      detach(audio);
      cleanupAudio(audio);
      current = null;
    }
    setState("error", job.key);
    if (!disposed && !blockedJob) pump();
  }

  async function pump() {
    if (pumping || disposed || current || blockedJob) return;
    const job = queued.shift();
    if (!job) {
      if (state !== "idle") setState("idle");
      return;
    }
    pumping = true;
    try {
      await startJob(job, job.manual);
    } finally {
      pumping = false;
      if (!disposed && !current && !blockedJob) pump();
    }
  }

  function enqueue(job) {
    if (disposed || !job || !job.key || !job.url) return false;
    const key = String(job.key);
    if (seen.has(key)) return false; // 同一消息（含双渲染重复入队）只播一次
    seen.add(key);
    queued.push({
      key,
      url: String(job.url),
      title: String(job.title || "语音"),
      attempts: 0,
      manual: false,
    });
    while (queued.length > maxJobs) queued.shift();
    pump();
    return true;
  }

  function toggle(job) {
    if (disposed || !job || !job.key) return;
    const key = String(job.key);
    if (current && current.job.key === key) {
      const a = current.audio;
      if (a && a.paused === false) {
        try {
          a.pause();
        } catch {
          /* ignore */
        }
      } else if (a) {
        try {
          const p = a.play(); // 用户手势内续播
          if (p && typeof p.catch === "function") p.catch(() => {});
        } catch {
          /* ignore */
        }
      }
      return;
    }
    if (blockedJob && blockedJob.key === key) {
      const j = blockedJob;
      blockedJob = null;
      queued.unshift({ ...j, manual: true }); // 手势内恢复被拦截的这条
      pump();
      return;
    }
    // 手动播放：插队最前；正在播的旧任务让位（暂停语义，不算 error）
    if (!job.url) return;
    dropCurrent("paused", current ? current.job.key : null);
    queued.unshift({
      key,
      url: String(job.url),
      title: String(job.title || "语音"),
      attempts: 0,
      manual: true,
    });
    pump();
  }

  /** 必须在真实用户手势处理器内调用：恢复被自动播放策略拦截的任务一次。 */
  function unlock() {
    if (disposed || !blockedJob || current) return;
    const job = blockedJob;
    blockedJob = null;
    pumping = true;
    startJob(job, job.manual || false).finally(() => {
      pumping = false;
      if (!disposed && !current && !blockedJob) pump();
    });
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    blockedJob = null;
    queued.length = 0;
    dropCurrent("disposed", null);
    if (state !== "disposed") setState("disposed");
  }

  function snapshot() {
    return { state, key: stateKey };
  }

  return { enqueue, toggle, unlock, snapshot, dispose, has: (key) => seen.has(String(key)) };
}

/* ==================== DOM 构建器（浏览器环境；Node 测试经极简 DOM 桩覆盖行为面） ==================== */

/** 静态常量 SVG（无任何插值/服务端文本，innerHTML 无注入面）。 */
const PICTURE_SVG =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4.5" width="18" height="15" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="9" cy="10" r="1.7" fill="currentColor"/><path d="M5.5 17.5l4.2-4.2 3 3 2.6-2.6 3.2 3.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></svg>';
const SPEAKER_SVG =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4 9.5v5h3.4L12 18.6V5.4L7.4 9.5H4z"/><path fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" d="M15 9.3a4 4 0 0 1 0 5.4"/><path fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" d="M17.6 6.9a7.4 7.4 0 0 1 0 10.2"/></svg>';

export function speakerIconSvg() {
  return SPEAKER_SVG;
}

/** 图片就绪判定：优先 decode()（解码成功才算「真的可展示」，坏损 blob 与
 * 网络失败同路兜底）；老内核无 decode 时回落一次性 load/error 事件。
 * 关键顺序：先挂 load/error 再赋 src —— 缓存命中时 load 可能早于监听。 */
function whenImageReady(img, src) {
  return new Promise((resolve, reject) => {
    let done = false;
    const ok = () => finish(null);
    const bad = () => finish(new Error("图片解码失败"));
    const finish = (err) => {
      if (done) return;
      done = true;
      img.removeEventListener("load", ok);
      img.removeEventListener("error", bad);
      if (err) reject(err);
      else resolve();
    };
    img.addEventListener("load", ok);
    img.addEventListener("error", bad);
    img.src = src;
    if (typeof img.decode === "function") {
      try {
        const p = img.decode();
        if (p && typeof p.then === "function") p.then(ok, bad);
      } catch {
        bad();
      }
    }
  });
}

/**
 * 认证图片媒体节点：先返回「真正 loading 占位」（微光、尺寸固定、aria-hidden
 * 不进读屏），blob 拉取 + 完整解码成功后才原位换入 img —— 加载全程绝不闪
 * alt 兜底文本/损坏图标。任一步失败交给 onFail；无 src 时不创建 img、不发请求。
 */
export function mountImagePart(
  part,
  loader,
  { className = "msg-media-img", loadingClassName = "msg-media-loading", onFail = null } = {}
) {
  const ph = document.createElement("span");
  ph.className = `${loadingClassName} skel`;
  ph.setAttribute("aria-hidden", "true");
  const ic = document.createElement("span");
  ic.className = "ph-ic";
  ic.innerHTML = PICTURE_SVG; // 静态常量 SVG，无注入面
  ph.appendChild(ic);

  const fail = () => {
    if (typeof onFail === "function") onFail(ph);
    else ph.remove(); // 气泡等无兜底场景：整块自撤，绝不留破损图标
  };

  const src = String((part && part.src) || "");
  if (!src || !loader || typeof loader.objectUrl !== "function") {
    Promise.resolve().then(fail); // 无 src：不插 img、不发请求，占位可读兜底
    return ph;
  }

  const img = document.createElement("img");
  img.className = className;
  const altTitle = cleanMediaTitle(part.title);
  img.alt = altTitle || "图片";
  img.title = altTitle;
  img.decoding = "async"; // 懒加载无意义：本就全量就绪后才入 DOM
  let settled = false; // 成功/失败只走一次，迟到对侧结果不再动 DOM
  loader.objectUrl(src).then(
    (obj) => {
      if (settled) return;
      settled = true;
      whenImageReady(img, obj).then(
        () => {
          img.classList.add("is-ready");
          ph.replaceWith(img); // 解码成功才原位亮图
        },
        () => {
          try {
            img.removeAttribute("src");
          } catch {
            /* ignore */
          }
          fail(); // 坏损 blob 与网络失败同路：可读占位/自撤，不露破图
        }
      );
    },
    () => {
      if (settled) return;
      settled = true;
      fail();
    }
  );
  return ph;
}

/** 「图片/语音未缓存」「图片加载失败」占位（未缓存不发请求；描述截断可读）。 */
export function mountPlaceholderPart(part) {
  const box = document.createElement("div");
  box.className = "msg-media-ph";
  const desc = cleanMediaTitle(part.title);
  box.title = desc || (part.failed ? "图片加载失败" : "本机未缓存");
  const ic = document.createElement("span");
  ic.className = "ph-ic";
  ic.innerHTML = part.voice ? SPEAKER_SVG : PICTURE_SVG;
  const label = document.createElement("span");
  label.textContent = part.voice ? "语音未缓存" : part.failed ? "图片加载失败" : "图片未缓存";
  box.appendChild(ic);
  box.appendChild(label);
  if (desc) {
    const small = document.createElement("small");
    small.textContent = desc.length > 24 ? `${desc.slice(0, 24)}…` : desc;
    box.appendChild(small);
  }
  return box;
}
