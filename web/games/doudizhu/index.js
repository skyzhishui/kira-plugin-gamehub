/* games/doudizhu/index.js — 斗地主页面模块（目录式模块：web/games/doudizhu/）。
 *
 * 模块契约与五子棋/黑白棋相同（games/registry.js 动态加载）：
 *   default { kind, moveHint, mount(host, { onPlace, meta }) → { update(match, opts), destroy() } }
 *
 * 规则零实现：叫分/出牌/牌型/压牌/计分全部读服务端 view；前端只把点选牌
 * 拼成点数文本提交（见 table.js / logic.js）。三座位的头像、身份、思考、
 * 气泡由 match.js 统一按座位驱动渲染（seat_drivers / nori_seat）。
 */

import { mountTable } from "./table.js";

export * from "./logic.js";

export default {
  kind: "doudizhu",
  moveHint: "点牌选中再出牌；叫分时点分数。没头绪可以看提示。",

  mount(host, { onPlace, meta } = {}) {
    // 斗地主不需要 #board-meta（局数/倍数在牌桌顶部），清空并保持隐藏，
    // 避免沿用黑白棋的计数条槽位。
    if (meta) {
      meta.textContent = "";
      meta.className = "board-meta hidden";
    }
    const table = mountTable(host, { onPlace });
    return {
      /** match: MatchView；opts: { canAct, busy, over, delegated } */
      update(match, opts = {}) {
        table.update(match, opts);
      },
      destroy() {
        table.destroy();
      },
    };
  },
};