/* 轻量 toast：textContent 渲染（永不 innerHTML），aria-live 播报，自动消失。 */

let root = null;

function ensureRoot() {
  if (!root || !root.isConnected) {
    root = document.createElement("div");
    root.className = "toast-root";
    root.setAttribute("aria-live", "polite");
    root.setAttribute("role", "status");
    document.body.appendChild(root);
  }
  return root;
}

/**
 * toast(message, kind?, ms?)
 * kind: "info" | "ok" | "error"
 */
export function toast(message, kind = "info", ms = 3200) {
  const r = ensureRoot();
  while (r.children.length >= 3) r.firstElementChild.remove();
  const t = document.createElement("div");
  t.className = `toast ${kind === "ok" || kind === "error" ? kind : "info"}`;
  t.textContent = String(message == null ? "" : message);
  r.appendChild(t);
  setTimeout(() => {
    t.classList.add("out");
    setTimeout(() => t.remove(), 200);
  }, Math.max(800, ms));
  return t;
}
