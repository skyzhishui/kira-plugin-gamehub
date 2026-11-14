/* shared/confetti.js — 小型彩纸爆点（canvas，≤80 片，devicePixelRatio 正确）。
 * - 只用 transform 语义的 canvas 绘制，无布局抖动；pointer-events:none 不拦截交互
 * - 页面 hidden 时 rAF 自动停摆，这里再显式冻结物理，避免恢复后瞬移
 * - prefers-reduced-motion: reduce 时直接不生成
 */

const COLORS = ["#5db9f5", "#e2a8f6", "#ffffff", "#f5b84f", "#c87eee"];

function motionReduced() {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

export function confettiBurst(host, { count = 72, duration = 2400 } = {}) {
  if (!host || motionReduced()) return null;
  const n = Math.max(1, Math.min(80, count));

  const canvas = document.createElement("canvas");
  canvas.className = "fx-confetti";
  canvas.setAttribute("aria-hidden", "true");
  host.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  let w = 0;
  let h = 0;
  const fit = () => {
    const rect = host.getBoundingClientRect();
    w = Math.max(1, rect.width);
    h = Math.max(1, rect.height);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
  };
  fit();

  // 从盘面中心向上抛洒
  const pieces = [];
  for (let i = 0; i < n; i++) {
    const ang = (-30 - Math.random() * 120) * (Math.PI / 180); // -30°..-150°
    const speed = 3.2 + Math.random() * 5.5;
    pieces.push({
      x: w / 2 + (Math.random() - 0.5) * 40,
      y: h / 2 + (Math.random() - 0.5) * 30,
      vx: Math.cos(ang) * speed,
      vy: Math.sin(ang) * speed - 2.2,
      rot: Math.random() * Math.PI * 2,
      vr: (Math.random() - 0.5) * 0.28,
      size: 5 + Math.random() * 6,
      color: COLORS[i % COLORS.length],
      wobble: Math.random() * Math.PI * 2,
    });
  }

  const t0 = performance.now();
  let raf = 0;
  const GRAVITY = 0.14;
  const DRAG = 0.988;

  function frame(now) {
    raf = 0;
    if (!canvas.isConnected) return;
    const t = now - t0;
    if (t >= duration) {
      canvas.remove();
      return;
    }
    if (!document.hidden) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const fade = t > duration - 420 ? Math.max(0, (duration - t) / 420) : 1;
      for (const p of pieces) {
        p.vy += GRAVITY;
        p.vx *= DRAG;
        p.vy *= DRAG;
        p.x += p.vx;
        p.y += p.vy;
        p.rot += p.vr;
        p.wobble += 0.1;
        ctx.save();
        ctx.globalAlpha = fade * (0.75 + 0.25 * Math.sin(p.wobble));
        ctx.translate(p.x, p.y);
        ctx.rotate(p.rot);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.size / 2, -p.size / 4, p.size, p.size / 2);
        ctx.restore();
      }
    }
    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);
  return { stop: () => { if (raf) cancelAnimationFrame(raf); canvas.remove(); } };
}
