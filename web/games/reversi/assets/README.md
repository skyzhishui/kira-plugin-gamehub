# 黑白棋素材

本目录当前不含任何图片素材：黑白棋的大厅封面由 `games/registry.js` 的候选链
（`./games/reversi/assets/card-reversi.png|svg` → `./assets/card-reversi.png`）
探测，全部缺失时卡片自动留一块柔和牌面占位（不塌卡、不手画 SVG 冒充正式素材）。

角色插画不进本仓库：KiraAI 的人设由用户配置，插件前端只保留通用装饰素材
（座位头像统一用公共 `web/assets/agent-avatar.png`）。棋盘本身由
`games/reversi/board.js` 在 canvas 上绘制，不依赖位图。