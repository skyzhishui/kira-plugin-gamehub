/* atmo.js — 大厅/对局页共用的动态氛围层（样式与动画全在 app.css 的 .fx-atmo 系列）。
 *
 * 这里只负责注入节点：极光/星云 blob ×3、日晕/月光、星点（含大星与菱形星）、彩屑。
 * - 全部动画都是 CSS 关键帧，只动 transform/opacity，JS 没有逐帧循环；
 * - prefers-reduced-motion: reduce 时不注入任何节点（纯渐变底仍由 body::before 提供），
 *   并监听系统开关即时重建；
 * - 大厅（body.lobby）氛围全开；对局页节点减半 + 整层降透明（见 app.css）。
 */

const reduced = matchMedia("(prefers-reduced-motion: reduce)");

const rand = (min, max) => min + Math.random() * (max - min);

function fx(cls, vars = {}) {
  const n = document.createElement("i");
  n.className = cls;
  for (const [k, v] of Object.entries(vars)) n.style.setProperty(k, v);
  return n;
}

/* 星点：大小/位置/闪烁周期全部错峰（负延迟让开场就处于半闪烁状态） */
function buildStars(root, count) {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const hi = Math.random() < 0.14; // 少量大星带光晕
    const dia = !hi && i % 9 === 4; // 偶尔一颗菱形小星
    const s = dia ? rand(3.5, 5.5) : hi ? rand(2.6, 3.4) : rand(1.2, 2.3);
    frag.appendChild(fx(`fx-star s${1 + (i % 3)}${hi ? " hi" : ""}${dia ? " dia" : ""}`, {
      "--s": `${s.toFixed(1)}px`,
      "--x": `${rand(1, 99).toFixed(2)}%`,
      "--y": `${rand(0, 92).toFixed(2)}%`,
      "--o": rand(0.35, 0.95).toFixed(2),
      "--td": `${rand(2.6, 6.8).toFixed(2)}s`,
      "--tdel": `${rand(-7, 0).toFixed(2)}s`,
    }));
  }
  root.appendChild(frag);
}

/* 彩屑：只在浅色主题可见（CSS 里 dark 隐藏），慢速飘落 + 轻微侧摆 */
function buildMotes(root, count) {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const round = Math.random() < 0.6;
    frag.appendChild(fx(`fx-mote c${1 + (i % 5)}`, {
      "--s": `${rand(5, 8).toFixed(1)}px`,
      "--r": round ? "50%" : "2.5px",
      "--x": `${rand(2, 98).toFixed(2)}%`,
      "--mo": rand(0.45, 0.8).toFixed(2),
      "--sway": `${rand(1.5, 4).toFixed(2)}vw`,
      "--fd": `${rand(17, 34).toFixed(1)}s`,
      "--fdel": `${rand(-34, 0).toFixed(1)}s`, // 负延迟：开场即在半空，不留空窗
    }));
  }
  root.appendChild(frag);
}

function build() {
  document.querySelectorAll(".fx-atmo").forEach((n) => n.remove());
  if (reduced.matches) return;
  const lobby = document.body.classList.contains("lobby");
  const root = document.createElement("div");
  root.className = "fx-atmo";
  root.setAttribute("aria-hidden", "true");
  root.appendChild(fx("fx-blob fx-blob-a"));
  root.appendChild(fx("fx-blob fx-blob-b"));
  root.appendChild(fx("fx-blob fx-blob-c"));
  root.appendChild(fx("fx-orb"));
  buildStars(root, lobby ? 54 : 24);
  if (lobby) buildMotes(root, 14);
  document.body.prepend(root);
}

build();
if (typeof reduced.addEventListener === "function") reduced.addEventListener("change", build);
