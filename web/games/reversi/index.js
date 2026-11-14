/* games/reversi/index.js — 黑白棋页面模块（目录式模块：web/games/reversi/）。
 *
 * 模块契约与 games/gomoku.js 相同（games/registry.js 动态加载）：
 *   default { kind, moveHint, mount(host, { onPlace, meta }) → { update(match, opts), destroy() } }
 *
 * 本目录收纳黑白棋专属前端代码与素材（board/logic/assets）；公共 registry 只负责
 * 按 kind 找到模块，不把棋种规则或视觉塞进公共文件。
 *
 * 规则零实现：合法点/翻子/终局/胜负全部读服务端 view（见 docs/dev/web-mode-contract.md）。
 * 用户可见文案不出现技术词；陪练/对手身份由 match.js 按模式渲染。
 */

import { ReversiBoard } from "./board.js";
import { coordName, metaText, normalizeSize, youOf } from "./logic.js";

export * from "./logic.js";

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = String(text);
  return node;
}

/**
 * 计数条：黑/白子数 + 手数（第一行）、pass/最后一手说明（第二行，高度恒定）。
 * 槽位由 match.html 提供（#board-meta），五子棋不显示该槽位。
 */
function buildMeta(slot) {
  if (!slot) return { update() {}, destroy() {} };

  slot.className = "board-meta rv-meta";
  slot.classList.remove("hidden");
  slot.textContent = "";

  const row = el("div", "rv-counts");
  const youEl = el("span", "rv-you", "观战");
  row.appendChild(youEl);

  const blackChip = el("span", "rv-count b");
  blackChip.appendChild(el("i", "rv-disc b"));
  blackChip.appendChild(document.createTextNode("黑 "));
  const blackNum = el("b", null, "0");
  blackChip.appendChild(blackNum);
  row.appendChild(blackChip);

  const whiteChip = el("span", "rv-count w");
  whiteChip.appendChild(el("i", "rv-disc w"));
  whiteChip.appendChild(document.createTextNode("白 "));
  const whiteNum = el("b", null, "0");
  whiteChip.appendChild(whiteNum);
  row.appendChild(whiteChip);

  const movesEl = el("span", "rv-moves", "尚未落子");
  row.appendChild(movesEl);
  slot.appendChild(row);

  const noteEl = el("div", "rv-note");
  noteEl.setAttribute("role", "status");
  noteEl.setAttribute("aria-live", "polite");
  slot.appendChild(noteEl);

  return {
    update(view, you) {
      const info = metaText(view, you);
      youEl.textContent = info.youText;
      youEl.classList.toggle("spectate", !(you === 1 || you === 2));
      blackNum.textContent = String(info.black);
      whiteNum.textContent = String(info.white);
      movesEl.textContent = info.movesText;
      if (noteEl.textContent !== info.note) noteEl.textContent = info.note;
      const passed = Number(view && view.passed);
      noteEl.classList.toggle("pass", passed === 1 || passed === 2);
      slot.dataset.you = String(you || "");
      slot.dataset.moves = String(Math.max(0, Math.trunc(Number(view && view.moves) || 0)));
    },
    destroy() {
      slot.textContent = "";
      slot.className = "board-meta hidden";
    },
  };
}

export default {
  kind: "reversi",
  moveHint: "点击亮点翻子（如 D6）；方向键移动光标，回车落子。",

  mount(host, { onPlace, meta } = {}) {
    let size = 8;
    const board = new ReversiBoard(host, {
      onPlace: (p) => {
        if (typeof onPlace !== "function") return;
        // 坐标名与后端同一套（列 A-H，行 1 在底部）；服务器仍是合法性唯一裁判
        onPlace({ x: p.x, y: p.y, move: coordName(p.x, p.y, size) });
      },
    });
    const strip = buildMeta(meta);

    return {
      /** match: MatchView；opts: { canAct, busy, over }（与五子棋一致） */
      update(match, { canAct, busy, over } = {}) {
        const view = match && match.view;
        if (!view) return;
        size = normalizeSize(view.size);
        const you = youOf(view, match);
        const ended = !!over || !!view.over;
        const myTurn = (you === 1 || you === 2) && Number(view.toPlay) === you;
        board.update(view, {
          canPlay: !!(canAct && myTurn && !busy && !ended),
          you,
          over: ended,
        });
        strip.update(view, you);
      },
      destroy() {
        board.destroy();
        strip.destroy();
      },
    };
  },
};