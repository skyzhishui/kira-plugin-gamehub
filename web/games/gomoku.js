/* games/gomoku.js — 五子棋模块：坐标编解码 + 棋盘挂载。
 *
 * 服务端视图约定：view = { size, cells:[0|1|2], toPlay, moves, last?, winLine?, full?, you }
 * - cells 索引 y*size+x，y=0 为最上排；1=黑，2=白
 * - 坐标名：列 A 起（左→右）、行号自下向上 1 起，如 "H8"
 * - view.you 为观棋者执子（1|2）；缺失时回退 me(座位号)+1
 */

import { GomokuBoard, GTP_COLUMNS, gtpXOf } from "../shared/board.js";

export function coordName(x, y, size) {
  // Column letters must skip I (same alphabet as the backend); otherwise x>=8 lands
  // one column to the left on the backend and x===8 becomes an illegal coordinate
  // (the backend reports it cannot parse the move: I8).
  return `${GTP_COLUMNS[x] || "?"}${size - y}`;
}

export function parseCoord(text, size) {
  const m = /^([A-Za-z])(\d{1,2})$/.exec(String(text == null ? "" : text).trim());
  if (!m || !size) return null;
  const x = gtpXOf(m[1]);
  const y = size - parseInt(m[2], 10);
  if (x < 0 || x >= size || y < 0 || y >= size) return null;
  return { x, y };
}

/** 观棋者执子：view.you（1|2）优先，回退座位号 me+1；观战返回 0 */
function stoneOf(match, view) {
  const you = view ? +view.you : 0;
  if (you === 1 || you === 2) return you;
  const me = match && typeof match.me === "number" ? match.me : -1;
  return me === 0 || me === 1 ? me + 1 : 0;
}

export default {
  kind: "gomoku",
  moveHint: "点击棋盘交叉点落子（坐标如 H8）；键盘方向键移动光标，回车落子。",

  mount(host, { onPlace }) {
    let size = 15;
    const board = new GomokuBoard(host, {
      onPlace: (p) => {
        if (typeof onPlace === "function") onPlace({ x: p.x, y: p.y, move: coordName(p.x, p.y, size) });
      },
    });
    return {
      /** match: MatchView；opts: { canAct, busy, over } */
      update(match, { canAct, busy, over } = {}) {
        const view = match && match.view;
        if (!view) return;
        const n = +view.size;
        if (Number.isFinite(n) && n > 1) size = n;
        const you = stoneOf(match, view);
        const myTurn = (+view.toPlay === 1 || +view.toPlay === 2) && +view.toPlay === you;
        board.update(view, {
          canPlay: !!(canAct && myTurn && !busy),
          youStone: you,
          over: !!over,
        });
      },
      destroy() {
        board.destroy();
      },
    };
  },
};
