/* shared/board.js — GomokuBoard：canvas 棋盘渲染与交互（宿主主 agent 主题 · 动效版）。
 *
 * - devicePixelRatio 正确缩放，ResizeObserver 驱动重绘（棋盘尺寸取宿主容器短边）
 * - 悬停显示预落子并轻微呼吸；键盘（方向键 + 回车/空格）可移动光标并落子
 * - 落子：下落 + 挤压回弹（squash & bounce）+ 落点扩散圆环；最后一手脉冲光晕
 * - 胜利五连：描边流动（lineDashOffset）+ 粉色辉光；最后一手标记为对比色圆点
 * - 动效只动 canvas 内部绘制（无布局抖动）；rAF 循环在页面 hidden 时自动停摆，
 *   prefers-reduced-motion 下全部退化为静态绘制
 * - 木纹线色取自页面 CSS 变量（--board-* / --win-glow / --sky-dark），双主题天然生效
 * - 视图兼容：last/winLine 接受 索引 | [x,y] | {x,y} | "H8" 字符串
 */

/* GTP column letters: start at A, skip I.
 * Note this is a **superset** of the backend: engine/coords.py COLUMNS is A..T
 * (19 columns), while the front end keeps the historical fixed alphabet up to Z
 * (25 columns). The first 19 positions match character for character, so coordinate
 * encoding within 19 lines is exactly equivalent, including the no-I rule.
 * Encoding, decoding and coordinate labels must all use this one mapping; otherwise
 * every column past J shifts by one and I becomes an illegal coordinate the backend
 * cannot parse. */
export const GTP_COLUMNS = "ABCDEFGHJKLMNOPQRSTUVWXYZ";

export function gtpXOf(ch) {
  return GTP_COLUMNS.indexOf(String(ch || "").toUpperCase());
}

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

/* 落子姿态曲线：下落(T1) → 挤压(T2) → 回弹沉降(T3)；dy 单位 = cell 比例 */
const DROP_T1 = 170;
const DROP_T2 = 245;
const DROP_T3 = 390;

function dropPose(dt) {
  if (dt <= 0) return { dy: -0.95, sx: 0.5, sy: 0.5 };
  if (dt < DROP_T1) {
    const p = dt / DROP_T1;
    const e = p * p; // ease-in 下落
    return { dy: -0.95 * (1 - e), sx: 0.5 + 0.5 * p, sy: 0.5 + 0.5 * p };
  }
  if (dt < DROP_T2) {
    const q = Math.sin(((dt - DROP_T1) / (DROP_T2 - DROP_T1)) * Math.PI);
    return { dy: 0, sx: 1 + 0.3 * q, sy: 1 - 0.32 * q }; // 挤压
  }
  if (dt < DROP_T3) {
    const p = (dt - DROP_T2) / (DROP_T3 - DROP_T2);
    const w = Math.sin(p * Math.PI * 2) * (1 - p) * 0.14; // 小回弹
    return { dy: 0, sx: 1 - w * 0.6, sy: 1 + w };
  }
  return { dy: 0, sx: 1, sy: 1, done: true };
}

export class GomokuBoard {
  /** host: 容器元素（棋盘取其短边为边长）；onPlace({x, y}) */
  constructor(host, { onPlace } = {}) {
    this.host = host;
    this.onPlace = typeof onPlace === "function" ? onPlace : () => {};
    this.canvas = document.createElement("canvas");
    this.canvas.className = "board-canvas";
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute("role", "img");
    this.canvas.setAttribute("aria-label", "棋盘。方向键移动光标，回车落子");
    host.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d");

    this.view = null;
    this.opts = { canPlay: false, youStone: 0, over: false };
    this.side = 0;
    this.dpr = 1;
    this.geom = null;
    this.hover = null;
    this.cursor = null;
    this.ripples = []; // { x, y, t0 }
    this._lastKey = "";
    this._dropT0 = -1;
    this._rippleDone = true;
    this._raf = 0;
    this.destroyed = false;
    this._reduced = motionReduced();

    this._step = this._step.bind(this);

    this._ro = new ResizeObserver(() => this._fit());
    this._ro.observe(host);

    this.canvas.addEventListener("pointermove", (e) => this._onMove(e));
    this.canvas.addEventListener("pointerleave", () => this._setHover(null));
    this.canvas.addEventListener("click", (e) => this._onClick(e));
    this.canvas.addEventListener("keydown", (e) => this._onKey(e));
    this.canvas.addEventListener("blur", () => this._render(performance.now()));
  }

  /** 更新视图；opts: { canPlay, youStone(1|2), over } */
  update(view, opts = {}) {
    this.view = view;
    this.opts = { ...this.opts, ...opts };
    const lp = this._lastPoint();
    const key = lp ? `${lp.x},${lp.y}` : "";
    if (key !== this._lastKey) {
      this._lastKey = key;
      if (lp && !this._reduced) {
        this._dropT0 = performance.now();
        this._rippleDone = false;
      }
    }
    this.canvas.style.cursor = this._interactive() ? "pointer" : "default";
    if (this._reduced) {
      this._render(performance.now()); // 静态一次
    } else {
      this._ensureLoop();
    }
  }

  destroy() {
    this.destroyed = true;
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
    try {
      this._ro.disconnect();
    } catch {
      /* ignore */
    }
    this.canvas.remove();
  }

  // ------------------------------------------------------------ 动画循环

  _ensureLoop() {
    if (this._raf || this.destroyed) return;
    this._raf = requestAnimationFrame(this._step);
  }

  _step(now) {
    this._raf = 0;
    if (this.destroyed) return;
    this._render(now);
    this._raf = requestAnimationFrame(this._step); // 页面 hidden 时浏览器自动暂停 rAF
  }

  // ------------------------------------------------------------ 布局

  _fit() {
    if (this.destroyed) return;
    const rect = this.host.getBoundingClientRect();
    // 高分辨率/大屏下按可用短边铺满，上限放宽到 900，棋子不再显得过小。
    const side = Math.max(0, Math.floor(Math.min(rect.width, rect.height, 900)));
    if (side < 80 || side === this.side) return;
    this.side = side;
    this.dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
    this.canvas.style.width = `${side}px`;
    this.canvas.style.height = `${side}px`;
    this.canvas.width = Math.round(side * this.dpr);
    this.canvas.height = Math.round(side * this.dpr);
    this._render(performance.now());
  }

  // ------------------------------------------------------------ 视图归一

  _sizeOf(view) {
    const n = view ? +view.size : 0;
    return Number.isFinite(n) && n > 1 ? n : 15;
  }

  _normPoint(p, size) {
    if (p == null) return null;
    if (typeof p === "number" && Number.isFinite(p)) {
      if (!size || p < 0 || p >= size * size) return null;
      return { x: p % size, y: Math.floor(p / size) };
    }
    if (Array.isArray(p) && p.length >= 2 && Number.isFinite(+p[0]) && Number.isFinite(+p[1])) {
      return { x: +p[0], y: +p[1] };
    }
    if (typeof p === "object" && Number.isFinite(+p.x) && Number.isFinite(+p.y)) {
      return { x: +p.x, y: +p.y };
    }
    if (typeof p === "string") {
      const m = /^([A-Za-z])(\d{1,2})$/.exec(p.trim());
      if (m && size) {
        const x = gtpXOf(m[1]);
        const y = size - parseInt(m[2], 10);
        if (x >= 0 && x < size && y >= 0 && y < size) return { x, y };
      }
    }
    return null;
  }

  _lastPoint() {
    const view = this.view;
    if (!view) return null;
    const size = this._sizeOf(view);
    let p = this._normPoint(view.last, size);
    if (!p && Array.isArray(view.moves) && view.moves.length) {
      p = this._normPoint(view.moves[view.moves.length - 1], size);
    }
    return p;
  }

  _winPoints() {
    const view = this.view;
    const wl = view && view.winLine;
    if (!Array.isArray(wl)) return null;
    const size = this._sizeOf(view);
    const pts = wl.map((p) => this._normPoint(p, size)).filter(Boolean);
    return pts.length >= 2 ? pts : null;
  }

  // ------------------------------------------------------------ 绘制

  _render(now) {
    if (this.destroyed || !this.ctx || !this.canvas.width || this.side < 80) return;
    const { ctx, side } = this;
    const view = this.view;
    const size = this._sizeOf(view);
    const labelSpace = side >= 240 ? Math.max(20, side * 0.058) : Math.max(10, side * 0.032);
    const cell = (side - labelSpace * 2) / (size - 1);
    const ox = labelSpace;
    const oy = labelSpace;
    this.geom = { size, cell, ox, oy };
    const reduced = this._reduced;

    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, side, side);

    const lineCol = cssVar(this.canvas, "--board-line", "rgba(255,255,255,.85)");
    const coordCol = cssVar(this.canvas, "--board-coord", "rgba(255,255,255,.72)");

    // 网格
    ctx.strokeStyle = lineCol;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i < size; i++) {
      const gx = Math.round(ox + i * cell) + 0.5;
      const gy = Math.round(oy + i * cell) + 0.5;
      ctx.moveTo(gx, oy);
      ctx.lineTo(gx, oy + (size - 1) * cell);
      ctx.moveTo(ox, gy);
      ctx.lineTo(ox + (size - 1) * cell, gy);
    }
    ctx.stroke();
    ctx.lineWidth = 1.5;
    ctx.strokeRect(ox - 0.5, oy - 0.5, (size - 1) * cell + 1, (size - 1) * cell + 1);

    // 星位
    if (size >= 9) {
      const c = Math.floor((size - 1) / 2);
      const e = size >= 13 ? 3 : 2;
      const starR = Math.max(2, cell * 0.09);
      ctx.fillStyle = lineCol;
      for (const [sx, sy] of [
        [e, e],
        [size - 1 - e, e],
        [e, size - 1 - e],
        [size - 1 - e, size - 1 - e],
        [c, c],
      ]) {
        ctx.beginPath();
        ctx.arc(ox + sx * cell, oy + sy * cell, starR, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // 坐标标签（空间足够时才画）
    if (cell >= 17 && side >= 240) {
      ctx.fillStyle = coordCol;
      ctx.font = `${Math.max(9, Math.round(cell * 0.3))}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (let i = 0; i < size; i++) {
        ctx.fillText(GTP_COLUMNS[i] || "", ox + i * cell, side - labelSpace / 2 + 1);
        ctx.fillText(String(size - i), labelSpace / 2, oy + i * cell);
      }
    }

    if (view && Array.isArray(view.cells)) {
      const r = cell * 0.46;
      const lastPt = this._lastPoint();

      // 落子扩散圆环
      if (!reduced && this.ripples.length) {
        this.ripples = this.ripples.filter((rp) => {
          const p = (now - rp.t0) / 520;
          if (p >= 1) return false;
          ctx.save();
          ctx.strokeStyle = cssVar(this.canvas, "--sky", "#5db9f5");
          ctx.globalAlpha = (1 - p) * 0.6;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(ox + rp.x * cell, oy + rp.y * cell, cell * (0.35 + p * 1.05), 0, Math.PI * 2);
          ctx.stroke();
          ctx.restore();
          return true;
        });
      }

      // 棋子（最后一手带 下落+挤压 姿态）
      let pose = null;
      if (lastPt && this._dropT0 > 0 && !reduced) {
        const dt = now - this._dropT0;
        pose = dropPose(dt);
        if (!this._rippleDone && dt >= DROP_T1 && lastPt) {
          this._rippleDone = true;
          this.ripples.push({ x: lastPt.x, y: lastPt.y, t0: now });
        }
        if (pose.done && this.ripples.length === 0) this._dropT0 = -1; // 动画结束，不再逐帧特判
      }

      view.cells.forEach((v, i) => {
        if (v !== 1 && v !== 2) return;
        const x = i % size;
        const y = (i / size) | 0;
        const isLast = lastPt && lastPt.x === x && lastPt.y === y;
        // 最后一手脉冲光晕（落地后呼吸）
        if (isLast && !reduced && (!pose || pose.dy === 0)) {
          const halo = 0.22 + 0.1 * Math.sin(now / 480);
          const gx = ox + x * cell;
          const gy = oy + y * cell;
          const grad = ctx.createRadialGradient(gx, gy, r * 0.4, gx, gy, cell * 0.85);
          grad.addColorStop(0, `rgba(255,255,255,${halo})`);
          grad.addColorStop(1, "rgba(255,255,255,0)");
          ctx.fillStyle = grad;
          ctx.beginPath();
          ctx.arc(gx, gy, cell * 0.85, 0, Math.PI * 2);
          ctx.fill();
        }
        this._stone(ox + x * cell, oy + y * cell, r, v, isLast ? pose : null);
      });

      // 最后一手标记：对比色圆点
      if (lastPt && (!pose || pose.dy === 0 || reduced)) {
        const v = view.cells[lastPt.y * size + lastPt.x];
        if (v === 1 || v === 2) {
          ctx.fillStyle = v === 1 ? "rgba(244,240,230,.92)" : "rgba(28,28,32,.85)";
          ctx.beginPath();
          ctx.arc(ox + lastPt.x * cell, oy + lastPt.y * cell, Math.max(2.5, cell * 0.11), 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // 胜利连线：辉光 + 描边流动
      const win = this._winPoints();
      if (win) {
        const glow = cssVar(this.canvas, "--win-glow", "rgba(240,154,232,.95)");
        const a = win[0];
        const b = win[win.length - 1];
        ctx.save();
        ctx.strokeStyle = glow;
        ctx.lineWidth = Math.max(3.5, cell * 0.14);
        ctx.lineCap = "round";
        ctx.shadowColor = glow;
        ctx.shadowBlur = cell * 0.6;
        if (!reduced) {
          ctx.setLineDash([cell * 0.55, cell * 0.38]);
          ctx.lineDashOffset = -(now / 26) % (cell * 0.93); // 流动
        }
        ctx.beginPath();
        ctx.moveTo(ox + a.x * cell, oy + a.y * cell);
        ctx.lineTo(ox + b.x * cell, oy + b.y * cell);
        ctx.stroke();
        ctx.restore();
        ctx.strokeStyle = glow;
        ctx.lineWidth = 2;
        for (const p of win) {
          ctx.beginPath();
          ctx.arc(ox + p.x * cell, oy + p.y * cell, cell * 0.46 + 2.5, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
    }

    // 悬停预落子（轻微呼吸）
    if (this.hover && this._canPlaceAt(this.hover.x, this.hover.y)) {
      const you = this.opts.youStone === 2 ? 2 : 1;
      const alpha = reduced ? 0.45 : 0.42 + 0.1 * Math.sin(now / 450);
      const prev = ctx.globalAlpha;
      ctx.globalAlpha = alpha;
      this._stone(this.geom.ox + this.hover.x * this.geom.cell, this.geom.oy + this.hover.y * this.geom.cell, this.geom.cell * 0.46, you, null);
      ctx.globalAlpha = prev;
    }

    // 键盘光标
    if (document.activeElement === this.canvas && this.cursor && this.geom) {
      const { ox: gx, oy: gy, cell: gc } = this.geom;
      ctx.save();
      ctx.strokeStyle = cssVar(this.canvas, "--sky-dark", "#2b8fd4");
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.arc(gx + this.cursor.x * gc, gy + this.cursor.y * gc, gc * 0.46 + 3, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }
  }

  /** (cx,cy) 画一颗子；pose 提供下落/挤压位移与缩放 */
  _stone(cx, cy, r, v, pose) {
    const ctx = this.ctx;
    ctx.save();
    if (pose) {
      ctx.translate(cx, cy + pose.dy * (this.geom ? this.geom.cell : 30));
      ctx.scale(pose.sx, pose.sy);
      cx = 0;
      cy = 0;
    }
    ctx.shadowColor = "rgba(30,50,80,.35)";
    ctx.shadowBlur = r * 0.55;
    ctx.shadowOffsetY = r * 0.16;
    const g = ctx.createRadialGradient(cx - r * 0.34, cy - r * 0.4, r * 0.12, cx, cy, r);
    if (v === 1) {
      g.addColorStop(0, "#5a626b");
      g.addColorStop(0.4, "#2a3138");
      g.addColorStop(1, "#10141a");
    } else {
      g.addColorStop(0, "#ffffff");
      g.addColorStop(0.55, "#eeeae1");
      g.addColorStop(1, "#cfc9bb");
    }
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(0.1, r), 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.lineWidth = 1;
    ctx.strokeStyle = v === 1 ? "rgba(255,255,255,.1)" : "rgba(60,50,35,.28)";
    ctx.stroke();
    ctx.restore();
  }

  // ------------------------------------------------------------ 交互

  _interactive() {
    return !!this.view && this.opts.canPlay && !this.opts.over;
  }

  _canPlaceAt(x, y) {
    const view = this.view;
    const g = this.geom;
    if (!view || !g || !Array.isArray(view.cells) || !this._interactive()) return false;
    if (x < 0 || y < 0 || x >= g.size || y >= g.size) return false;
    return view.cells[y * g.size + x] === 0;
  }

  _pointAt(px, py) {
    const g = this.geom;
    if (!g) return null;
    const x = Math.round((px - g.ox) / g.cell);
    const y = Math.round((py - g.oy) / g.cell);
    if (x < 0 || y < 0 || x >= g.size || y >= g.size) return null;
    const dx = px - (g.ox + x * g.cell);
    const dy = py - (g.oy + y * g.cell);
    if (Math.hypot(dx, dy) > g.cell * 0.48) return null;
    return { x, y };
  }

  _pointFromEvent(e) {
    const rect = this.canvas.getBoundingClientRect();
    // canvas 是 border-box（全局 * 设定）：6px 白框占用了 style 宽高，而画布绘制
    // 坐标按 side 计算。指针先去掉 border 落到内容盒，再等比映射回 side 空间，
    // 否则越靠右/下偏移越大（表现为落子跑到指针左边一格）。
    const cw = this.canvas.clientWidth || rect.width;
    const ch = this.canvas.clientHeight || rect.height;
    if (!cw || !ch) return null;
    const px = (e.clientX - rect.left - this.canvas.clientLeft) * (this.side / cw);
    const py = (e.clientY - rect.top - this.canvas.clientTop) * (this.side / ch);
    return this._pointAt(px, py);
  }

  _setHover(p) {
    const key = p ? `${p.x},${p.y}` : "";
    if (key === (this._hoverKey || "")) return;
    this._hoverKey = key;
    this.hover = p;
    if (this._reduced) this._render(performance.now());
  }

  _onMove(e) {
    if (!this._interactive()) {
      this._setHover(null);
      return;
    }
    this._setHover(this._pointFromEvent(e));
  }

  _onClick(e) {
    if (!this._interactive()) return;
    const p = this._pointFromEvent(e);
    if (!p || !this._canPlaceAt(p.x, p.y)) return;
    this.onPlace(p);
  }

  _onKey(e) {
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", " "].includes(e.key)) return;
    e.preventDefault();
    const g = this.geom;
    if (!g) return;
    if (!this.cursor) this.cursor = this._lastPoint() || { x: (g.size - 1) >> 1, y: (g.size - 1) >> 1 };
    if (e.key !== "Enter" && e.key !== " ") {
      const c = { ...this.cursor };
      if (e.key === "ArrowUp") c.y -= 1;
      else if (e.key === "ArrowDown") c.y += 1;
      else if (e.key === "ArrowLeft") c.x -= 1;
      else c.x += 1;
      c.x = Math.min(g.size - 1, Math.max(0, c.x));
      c.y = Math.min(g.size - 1, Math.max(0, c.y));
      this.cursor = c;
    } else if (this._canPlaceAt(this.cursor.x, this.cursor.y)) {
      this.onPlace(this.cursor);
    }
    this._render(performance.now());
  }
}
