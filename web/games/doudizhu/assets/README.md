# 斗地主牌桌素材

全部图片为本地生成的位图（PNG/JPG），页面只用 CSS 背景与 `<img>` 引用，
**不手画 SVG 冒充正式素材**，也不接外部 CDN / 字体。

角色插画不进本仓库：KiraAI 的人设由用户配置，插件前端只保留通用装饰素材
（座位头像统一用公共 `web/assets/agent-avatar.png`）。

| 文件 | 用途 |
| --- | --- |
| `table-wood.jpg` | 牌桌桌面：暖蜂蜜橡木俯视纹理 |
| `table-felt.jpg` | 桌面毡垫：粉蓝天空色绒面纹理 |
| `sticker-paw.png` | 贴纸：柔粉爪印（透明，顶部纸带与毡面点缀） |
| `sticker-sparkle.png` | 贴纸：柔粉星点（透明，毡面/结算点缀） |

样式落点：

- 牌桌/毡面/牌背/牌面质感都在 `web/games/doudizhu/table.css`（在 `app.css`
  之后加载，整块覆盖旧的 `.dz-*` 规则）。牌背与花牌/王无人像插画，牌背走
  CSS 渐变 + 花纹，花牌/王只显示点数与花色角标。
- 贴纸在 `table.js` 里以装饰 `<img>` 插入（`alt=""`、`aria-hidden`、
  `pointer-events:none`），加载失败会自动移除，不留破图。
- 目录内只放本游戏的专属素材，公共 `web/assets/` 保持通用资源。