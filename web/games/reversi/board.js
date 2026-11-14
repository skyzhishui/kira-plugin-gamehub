/* games/reversi/board.js — 黑白棋 canvas 棋盘（诺里主题 · 翻子动效版）。
 *
 * 纪律沿用 shared/board.js（五子棋）：
 * - devicePixelRatio 正确缩放；ResizeObserver 驱动重绘；绘制空间 = 画布内容盒，
 *   8 列 8 行始终是正方格（画布外框含 CSS border，先定外框再取内容宽）
 * - 合法点完全来自服务端 view.legal（本文件不实现规则，不做翻子计算）
 * - 点击/触屏落子：只有服务端标出的合法点可点；命中按整格（不是十字交点）
 * - 键盘：方向键移动光标，回车/空格在合法点落子
 * - 最后一手：粉色描边环；新子下落 + 被翻子 180° 翻转（颜色在中点互换）
 * - 动效只发生在 canvas 内（无布局抖动）；rAF 只在「动效进行中 / 轮到你」时跑，
 *   prefers-reduced-motion 全部退化为静态绘制
 * - 颜色取自页面 CSS 变量（--board-* / --pink / --pink-deep / --sky-dark），
 *   浅色/深色主题天然生效
 *
 * 可测性接缝（只读 DOM 属性，不承载状态机）：canvas.dataset 暴露
 * size/toPlay/moves/last/legal/legalCount/you/over/canPlay/geom。
 */

import {
  COLUMNS,
  boardAriaLabel,
  cellIndex,
  coordName,
  isFreshMove,
  isPlayable,
  lastMoveIndex,
  legalMap,
  normalizeSize,
  pointOf,
} from "./logic.js";

const DROP_MS = 230;
const FLIP_MS = 330;
const TAU = Math.PI * 2;

function cssVar(canvas, name, fallback) {
  const v = getComputedStyle(canvas).getPropertyValue(name).trim();
  return v || fallback;
}

function motionReduced() {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

export class ReversiBoard {
  /** host: 容器元素（棋盘取其短边）；onPlace({x, y, index}) */
  constructor(host, { onPlace } = {}) {
    this.host = host;
    this.onPlace = typeof onPlace === "function" ? onPlace : () => {};
    this.canvas = document.createElement("canvas");
    this.canvas.className = "board-canvas rv-board";
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute("role", "img");
    this.canvas.setAttribute("aria-keyshortcuts", "ArrowUp ArrowDown ArrowLeft ArrowRight Enter Space");
    this.canvas.setAttribute("aria-label", "黑白棋棋盘");
    host.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d");

    this.view = null;
    this.opts = { canPlay: false, you: 0, over: false };
    this.legal = new Map();
    this.side = 0;
    this.avail = 0;
    this.dpr = 1;
    this.geom = null;
    this.hover = null;
    this.cursor = null;
    this._anim = null; // { t0, last, flipped:Set }
    this._raf = 0;
    this._commitAt = -1e9; // 首击不受「连点去重」影响
    this.destroyed = false;
    this._reduced = motionReduced();

    this._step = this._step.bind(this);
    this._onMove = this._onMove.bind(this);
    this._onLeave = this._onLeave.bind(this);
    this._onDown = this._onDown.bind(this);
    this._onUp = this._onUp.bind(this);
    this._onClick = this._onClick.bind(this);
    this._onKey = this._onKey.bind(this);

    this._ro = new ResizeObserver(() => this._fit());
    this._ro.observe(host);

    this.canvas.addEventListener("pointermove", this._onMove);
    this.canvas.addEventListener("pointerleave", this._onLeave);
    this.canvas.addEventListener("pointerdown", this._onDown);
    this.canvas.addEventListener("pointerup", this._onUp);
    this.canvas.addEventListener("click", this._onClick);
    this.canvas.addEventListener("keydown", this._onKey);
    // 首次尺寸测量放到下一帧：构造时宿主可能尚未布局（clientWidth=0 会让棋盘不出现）
    this._fitPending = requestAnimationFrame(() => {
      this._fitPending = 0;
      this._fit();
    });

    // 系统「减少动态效果」可在会话中切换：跟随并重绘（不重建棋盘）
    try {
      this._mq = window.matchMedia("(prefers-reduced-motion: reduce)");
      this._mqHandler = () => {
        this._reduced = this._mq.matches;
        if (this._reduced) {
          this._anim = null;
          this.canvas.dataset.animating = "0";
          if (this._raf) cancelAnimationFrame(this._raf);
          this._raf = 0;
        }
        this._render(performance.now());
        if (this._needsFrames(performance.now())) this._ensureLoop();
      };
      if (typeof this._mq.addEventListener === "function") {
        this._mq.addEventListener("change", this._mqHandler);
      } else if (typeof this._mq.addListener === "function") {
        this._mq.addListener(this._mqHandler);
      }
    } catch {
      this._mq = null;
    }
  }

  /** 更新视图；opts: { canPlay, you(1|2|0), over } */
  update(view, opts = {}) {
    const prev = this.view;
    this.view = view || null;
    this.opts = { ...this.opts, ...opts };
    this.legal = legalMap(this.view);
    if (isFreshMove(prev, this.view) && !this._reduced) {
      const flipped = new Set();
      const src = Array.isArray(this.view.flipped) ? this.view.flipped : [];
      for (const raw of src) {
        const i = Number(raw);
        if (Number.isInteger(i) && i >= 0) flipped.add(i);
      }
      this._anim = { t0: performance.now(), last: lastMoveIndex(this.view), flipped };
    } else {
      // 同局面/回退/首帧：不重播（reconnect 重放、聊天帧、代下状态帧都走这里）
      this._anim = null;
    }
    // 动效状态只读暴露给测试/调试（不参与逻辑）
    this.canvas.dataset.animating = this._anim ? "1" : "0";
    this.canvas.style.cursor = this._interactive() ? "pointer" : "default";
    this._syncAria();
    this._syncData();
    this._render(performance.now());
    if (this._needsFrames(performance.now())) this._ensureLoop();
    else if (this._raf) {
      cancelAnimationFrame(this._raf);
      this._raf = 0;
    }
  }

  destroy() {
    this.destroyed = true;
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
    if (this._fitPending) cancelAnimationFrame(this._fitPending);
    this._fitPending = 0;
    try {
      this._ro.disconnect();
    } catch {
      /* ignore */
    }
    try {
      if (this._mq) {
        if (typeof this._mq.removeEventListener === "function") {
          this._mq.removeEventListener("change", this._mqHandler);
        } else if (typeof this._mq.removeListener === "function") {
          this._mq.removeListener(this._mqHandler);
        }
      }
    } catch {
      /* ignore */
    }
    this.canvas.removeEventListener("pointermove", this._onMove);
    this.canvas.removeEventListener("pointerleave", this._onLeave);
    this.canvas.removeEventListener("pointerdown", this._onDown);
    this.canvas.removeEventListener("pointerup", this._onUp);
    this.canvas.removeEventListener("click", this._onClick);
    this.canvas.removeEventListener("keydown", this._onKey);
    this.canvas.remove();
  }

  // ------------------------------------------------------------ 布局

  _sizeOf() {
    return normalizeSize(this.view && this.view.size);
  }

  _fit() {
    if (this.destroyed) return;
    const rect = this.host.getBoundingClientRect();
    // 大屏按可用短边铺满，上限 900；窄屏仍由容器短边约束。
    const avail = Math.max(0, Math.floor(Math.min(rect.width || 0, rect.height || 0, 900)));
    if (avail < 80) return;
    const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
    this.avail = avail;
    this.dpr = dpr;
    // 外框（含 CSS border）定为可用短边；绘制坐标空间取内容盒 → 格子是正方形
    this.canvas.style.width = `${avail}px`;
    this.canvas.style.height = `${avail}px`;
    const side = Math.max(80, Math.floor(this.canvas.clientWidth || avail));
    if (side === this.side && this.canvas.width === Math.round(side * dpr)
        && this.canvas.height === Math.round(side * dpr)) {
      this._computeGeom();
      this._syncData();
      this._render(performance.now());
      return;
    }
    this.side = side;
    this.canvas.width = Math.round(side * dpr);
    this.canvas.height = Math.round(side * dpr);
    this._computeGeom();
    this._syncData();
    this._render(performance.now());
  }

  _computeGeom() {
    const size = this._sizeOf();
    const side = this.side || 0;
    if (!side) {
      this.geom = null;
      return;
    }
    // 边距随尺寸收紧：窄屏（320）也把格子留足，坐标标签仍可读
    const pad = Math.max(10, Math.round(side * 0.055));
    const cell = (side - pad * 2) / size;
    this.geom = { size, pad, cell, x0: pad, y0: pad, side };
  }

  // ------------------------------------------------------------ 动画循环

  _ensureLoop() {
    if (this._raf || this.destroyed || this._reduced) return;
    this._raf = requestAnimationFrame(this._step);
  }

  _step(now) {
    this._raf = 0;
    if (this.destroyed) return;
    if (this._anim && now - this._anim.t0 > FLIP_MS + 80) {
      this._anim = null;
      this.canvas.dataset.animating = "0";
    }
    this._render(now);
    if (this._needsFrames(now)) this._raf = requestAnimationFrame(this._step);
  }

  _needsFrames(now) {
    if (this.destroyed || this._reduced) return false;
    if (this._anim) return true;
    // 轮到你时合法点呼吸；对手回合/观战/终局静态，省电
    return this._interactive() && this.legal.size > 0;
  }

  // ------------------------------------------------------------ 交互状态

  _interactive() {
    return isPlayable(this.view, {
      you: this.opts.you,
      canAct: this.opts.canPlay,
      busy: false,
      over: this.opts.over,
    });
  }

  _pointFromEvent(e) {
    const rect = this.canvas.getBoundingClientRect();
    const cw = this.canvas.clientWidth || rect.width;
    const ch = this.canvas.clientHeight || rect.height;
    if (!cw || !ch) return null;
    const px = (e.clientX - rect.left - this.canvas.clientLeft) * (this.side / cw);
    const py = (e.clientY - rect.top - this.canvas.clientTop) * (this.side / ch);
    return this._pointAt(px, py);
  }

  _pointAt(px, py) {
    const g = this.geom;
    if (!g) return null;
    const x = Math.floor((px - g.x0) / g.cell);
    const y = Math.floor((py - g.y0) / g.cell);
    if (x < 0 || y < 0 || x >= g.size || y >= g.size) return null;
    return { x, y, index: cellIndex(x, y, g.size) };
  }

  _setHover(p) {
    const next = p && p.index >= 0 ? p : null;
    const key = next ? `${next.x},${next.y}` : "";
    if (key === (this._hoverKey || "")) return;
    this._hoverKey = key;
    this.hover = next;
    if (!this._raf) this._render(performance.now());
  }

  _onMove(e) {
    if (e.pointerType === "touch") return; // 触屏由 pointerdown 给一次反馈
    if (!this._interactive()) {
      this._setHover(null);
      return;
    }
    this._setHover(this._pointFromEvent(e));
  }

  _onLeave() {
    this._setHover(null);
  }

  _onDown(e) {
    if (!this._interactive()) return;
    if (e.pointerType === "touch") this._setHover(this._pointFromEvent(e));
  }

  /** 触屏落子：pointerup 即提交（不依赖浏览器合成 click，也不吃 300ms 点击延迟）。
   * 随后的合成 click 由 _commitAt 去重，不会重复落子。 */
  _onUp(e) {
    if (e.pointerType !== "touch") return;
    if (!this._interactive()) return;
    const p = this._pointFromEvent(e);
    if (!p || !this.legal.has(p.index)) return;
    this._commit(p);
  }

  _onClick(e) {
    if (!this._interactive()) return;
    const p = this._pointFromEvent(e);
    if (!p || !this.legal.has(p.index)) return;
    this._commit(p);
  }

  _commit(p) {
    const now = performance.now();
    if (now - this._commitAt < 350) return; // 双击/连点不重复提交（服务端仍兜底）
    this._commitAt = now;
    this._setHover(null);
    this.onPlace({ x: p.x, y: p.y, index: p.index });
  }

  _onKey(e) {
    const keys = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", " "];
    if (!keys.includes(e.key)) return;
    if (!this._interactive()) return; // 观战/对手回合不接键盘落子
    e.preventDefault();
    const g = this.geom;
    if (!g) return;
    if (!this.cursor) {
      const last = lastMoveIndex(this.view);
      const p = last >= 0 ? pointOf(last, g.size) : null;
      this.cursor = p || { x: (g.size - 1) >> 1, y: (g.size - 1) >> 1 };
    }
    if (e.key === "Enter" || e.key === " ") {
      const index = cellIndex(this.cursor.x, this.cursor.y, g.size);
      if (this.legal.has(index)) {
        this.cursor = { ...this.cursor };
        this._commit({ x: this.cursor.x, y: this.cursor.y, index });
      }
    } else {
      const c = { ...this.cursor };
      if (e.key === "ArrowUp") c.y -= 1;
      else if (e.key === "ArrowDown") c.y += 1;
      else if (e.key === "ArrowLeft") c.x -= 1;
      else c.x += 1;
      c.x = Math.min(g.size - 1, Math.max(0, c.x));
      c.y = Math.min(g.size - 1, Math.max(0, c.y));
      this.cursor = c;
    }
    this._syncAria();
    if (!this._raf) this._render(performance.now());
  }

  // ------------------------------------------------------------ 可测性 / 读屏

  _syncData() {
    const d = this.canvas.dataset;
    const view = this.view;
    const g = this.geom;
    d.kind = "reversi";
    d.size = String(this._sizeOf());
    d.toPlay = view && view.toPlay != null ? String(view.toPlay) : "";
    d.moves = String(Math.max(0, Math.trunc(Number(view && view.moves) || 0)));
    d.last =
      view && view.last != null && view.last !== "" && Number.isInteger(+view.last)
        ? String(+view.last)
        : "";
    d.legal = [...this.legal.keys()].sort((a, b) => a - b).join(",");
    d.legalCount = String(this.legal.size);
    d.you = this.opts.you === 1 || this.opts.you === 2 ? String(this.opts.you) : "";
    d.over = this.opts.over || (view && view.over) ? "1" : "0";
    d.canPlay = this.opts.canPlay ? "1" : "0";
    d.geom = g ? `${g.x0.toFixed(2)},${g.y0.toFixed(2)},${g.cell.toFixed(2)}` : "";
  }

  _syncAria() {
    const base = boardAriaLabel(this.view, this.opts.you);
    // 键盘光标位置补进读屏标签（坐标名与后端同一套）
    const cursor = this.cursor
      ? `光标 ${coordName(this.cursor.x, this.cursor.y, this._sizeOf())}，`
      : "";
    this.canvas.setAttribute("aria-label", cursor ? base.replace("方向键", `${cursor}方向键`) : base);
  }

  // ------------------------------------------------------------ 绘制

  _render(now) {
    if (this.destroyed || !this.ctx) return;
    if (!this.geom) this._computeGeom();
    const g = this.geom;
    if (!g || this.side < 80) return;
    const { ctx, side, dpr } = this;
    const { size, pad, cell } = g;
    const x0 = g.x0;
    const y0 = g.y0;
    const reduced = this._reduced;
    const view = this.view;
    const cells = view && Array.isArray(view.cells) ? view.cells : null;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, side, side);

    const lineCol = cssVar(this.canvas, "--board-line", "rgba(255,255,255,.85)");
    const coordCol = cssVar(this.canvas, "--board-coord", "rgba(255,255,255,.72)");
    const pink = cssVar(this.canvas, "--pink", "#e2a8f6");
    const pinkDeep = cssVar(this.canvas, "--pink-deep", "#c87eee");
    const skyDark = cssVar(this.canvas, "--sky-dark", "#2b8fd4");

    // 1) 极浅的棋盘格语气：交替格面 + 细网格，帮助读坐标又不抢棋子
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const alt = (x + y) % 2 === 1;
        ctx.fillStyle = alt ? "rgba(255,255,255,0.07)" : "rgba(255,255,255,0.14)";
        roundRect(ctx, x0 + x * cell + 1, y0 + y * cell + 1, cell - 2, cell - 2, cell * 0.16);
        ctx.fill();
      }
    }

    // 2) 盘面细线 + 内框
    ctx.strokeStyle = lineCol;
    ctx.globalAlpha = 0.4;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < size; i++) {
      ctx.moveTo(x0 + i * cell, y0);
      ctx.lineTo(x0 + i * cell, y0 + size * cell);
      ctx.moveTo(x0, y0 + i * cell);
      ctx.lineTo(x0 + size * cell, y0 + i * cell);
    }
    ctx.stroke();
    ctx.globalAlpha = 0.75;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(x0 - 3.5, y0 - 3.5, size * cell + 7, size * cell + 7);
    ctx.globalAlpha = 1;

    // 3) 坐标：列 A-H 在底、行 8-1 在左（行 1 在底部）
    if (cell >= 17) {
      ctx.fillStyle = coordCol;
      ctx.font = `${Math.max(9, Math.round(cell * 0.3))}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (let i = 0; i < size; i++) {
        ctx.fillText(COLUMNS[i] || "", x0 + (i + 0.5) * cell, side - pad / 2 + 0.5);
        ctx.fillText(String(size - i), pad / 2, y0 + (i + 0.5) * cell);
      }
    }

    const interactive = this._interactive();
    const you = this.opts.you === 2 ? 2 : 1;

    // 4) 合法点：只在本方回合绘制（观战/对手回合不暗示可点）
    if (interactive) {
      for (const [index, flips] of this.legal) {
        const p = pointOf(index, size);
        if (!p) continue;
        const cx = x0 + (p.x + 0.5) * cell;
        const cy = y0 + (p.y + 0.5) * cell;
        const phase = reduced ? 0.55 : 0.5 + 0.5 * Math.sin(now / 520 + index * 0.7);
        const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, cell * 0.46);
        glow.addColorStop(0, `rgba(226,168,246,${0.3 + 0.2 * phase})`);
        glow.addColorStop(1, "rgba(226,168,246,0)");
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(cx, cy, cell * 0.46, 0, TAU);
        ctx.fill();
        ctx.fillStyle = pinkDeep;
        ctx.beginPath();
        ctx.arc(cx, cy, cell * (0.085 + 0.02 * phase), 0, TAU);
        ctx.fill();
        // 小提示：翻子数只在悬停/光标时以环数暗示，常驻保持干净
        if (this.hover && this.hover.index === index && flips > 0) {
          ctx.strokeStyle = "rgba(255,255,255,.65)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(cx, cy, cell * 0.2, 0, TAU);
          ctx.stroke();
        }
      }
    }

    // 5) 悬停预览（整格柔光 + 半透明棋子）
    if (interactive && this.hover && this.legal.has(this.hover.index)) {
      const { x, y } = this.hover;
      ctx.save();
      ctx.fillStyle = "rgba(255,255,255,0.2)";
      roundRect(ctx, x0 + x * cell + 2, y0 + y * cell + 2, cell - 4, cell - 4, cell * 0.16);
      ctx.fill();
      ctx.restore();
      this._disc(x0 + (x + 0.5) * cell, y0 + (y + 0.5) * cell, cell * 0.4, you, { alpha: 0.34 });
    }

    // 6) 棋子（最后一手带下落姿态；被翻子做 180° 翻转）
    if (cells) {
      const last = lastMoveIndex(view);
      const anim = this._anim;
      const dropP = anim && !reduced ? Math.min(1, Math.max(0, (now - anim.t0) / DROP_MS)) : 1;
      for (let i = 0; i < cells.length; i++) {
        const color = cells[i];
        if (color !== 1 && color !== 2) continue;
        const p = pointOf(i, size);
        if (!p) continue;
        const cx = x0 + (p.x + 0.5) * cell;
        const cy = y0 + (p.y + 0.5) * cell;
        const r = cell * 0.4;

        if (anim && anim.flipped.has(i) && !reduced) {
          const fp = Math.min(1, Math.max(0, (now - anim.t0) / FLIP_MS));
          if (fp < 1) {
            const e = fp < 0.5 ? 2 * fp * fp : 1 - Math.pow(-2 * fp + 2, 2) / 2;
            const sx = Math.max(0.08, Math.abs(Math.cos(Math.PI * e)));
            const lift = -Math.sin(Math.PI * e) * cell * 0.08;
            const tint = e < 0.5 ? 3 - color : color;
            this._disc(cx, cy + lift, r, tint, { sx, sy: 1 + 0.05 * Math.sin(Math.PI * e) });
            continue;
          }
        }

        if (i === last && dropP < 1) {
          const e = 1 - Math.pow(1 - dropP, 3);
          const bounce = Math.sin(dropP * Math.PI * 2) * (1 - dropP) * 0.07;
          this._disc(cx, cy - (1 - e) * 0.5 * cell, r, color, {
            sx: 0.66 + 0.34 * e + bounce,
            sy: 0.66 + 0.34 * e - bounce,
            alpha: 0.45 + 0.55 * e,
          });
          continue;
        }

        this._disc(cx, cy, r, color);
      }
    }

    // 7) 最后一手描边环（两种主题下都清晰；reduced-motion 下静止）
    if (cells) {
      const last = lastMoveIndex(view);
      const p = last >= 0 ? pointOf(last, size) : null;
      const color = p ? cells[last] : 0;
      if (p && (color === 1 || color === 2)) {
        const cx = x0 + (p.x + 0.5) * cell;
        const cy = y0 + (p.y + 0.5) * cell;
        const breathe = reduced ? 0.8 : 0.72 + 0.16 * Math.sin(now / 420);
        ctx.save();
        ctx.strokeStyle = pinkDeep;
        ctx.globalAlpha = breathe;
        ctx.lineWidth = Math.max(1.8, cell * 0.055);
        ctx.shadowColor = pink;
        ctx.shadowBlur = cell * 0.32;
        ctx.beginPath();
        ctx.arc(cx, cy, cell * 0.33, 0, TAU);
        ctx.stroke();
        ctx.restore();
      }
    }

    // 8) 键盘光标：虚线方框（不在棋盘外扩，避免挤压棋盘）
    if (document.activeElement === this.canvas && this.cursor) {
      const { x, y } = this.cursor;
      ctx.save();
      ctx.strokeStyle = skyDark;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      roundRect(ctx, x0 + x * cell + 3, y0 + y * cell + 3, cell - 6, cell - 6, cell * 0.18);
      ctx.stroke();
      ctx.restore();
    }
  }

  /** 一颗棋子：黑/白渐变 + 柔阴影 + 顶部小高光（与五子棋同视觉语言）。 */
  _disc(cx, cy, r, color, { sx = 1, sy = 1, alpha = 1 } = {}) {
    const ctx = this.ctx;
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(cx, cy);
    if (sx !== 1 || sy !== 1) ctx.scale(sx, sy);
    if (sx > 0.34) {
      ctx.shadowColor = "rgba(30,50,80,.34)";
      ctx.shadowBlur = r * 0.6;
      ctx.shadowOffsetY = r * 0.16;
    }
    const grad = ctx.createRadialGradient(-r * 0.34, -r * 0.42, r * 0.1, 0, 0, r);
    if (color === 1) {
      grad.addColorStop(0, "#5a626b");
      grad.addColorStop(0.4, "#2a3138");
      grad.addColorStop(1, "#10141a");
    } else {
      grad.addColorStop(0, "#ffffff");
      grad.addColorStop(0.55, "#eeeae1");
      grad.addColorStop(1, "#cfc9bb");
    }
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(0, 0, Math.max(0.1, r), 0, TAU);
    ctx.fill();
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.lineWidth = 1;
    ctx.strokeStyle = color === 1 ? "rgba(255,255,255,.1)" : "rgba(60,50,35,.28)";
    ctx.stroke();
    // 顶部高光小亮斑：清新可爱，但不加额外装饰
    ctx.globalAlpha = alpha * (color === 1 ? 0.18 : 0.5);
    ctx.fillStyle = "#ffffff";
    ctx.beginPath();
    ctx.ellipse(-r * 0.3, -r * 0.42, r * 0.28, r * 0.16, -0.5, 0, TAU);
    ctx.fill();
    ctx.restore();
  }
}
