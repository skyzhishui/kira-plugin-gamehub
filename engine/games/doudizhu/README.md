# 斗地主规则实现边界

基线：SerenQi/rain-go `06e4b75` 的 doudizhu / doudizhu-cards / doudizhu-bot。
原项目 MIT 授权见相邻 `LICENSE.rain-go`。

## 规则等价性

- 叫分制、重发、17×3+3底牌、14种牌型、两过清圈、春天/反春与1/3/6局积分。
- `rng` 使用既有 mulberry32，seed 发牌与原项目一致。
- 对原 TypeScript 使用 Node 原生 TS 运行，以4个seed的完整动作轨迹验证每步完整state相等，共95动作。
- 25个seed完整bot对局采样852个出牌局面，合法move集合逐项相等。
- 固化回归：seed=42的发牌向量、seed=0/42/123的完整对局终态摘要。
- 原项目动作日志仅牌型名，本插件 `MoveResult.log` 增加公开已出牌文本，便于回放；不含私牌。

## 刻意差异

- 默认1局（原项目3局），用户可在开局选择1/3/6。
- bot叫分阈值与防重发死循环策略来自原项目，出牌策略使用更小的非透视启发式，
  不宣称逐步选牌等价。规则状态机与合法动作集合等价不代表同样棋力。
- 视图显式白名单，绝不展开state：不公开rng/三家手牌表；观战无hand/hints。
- 模块只管规则，诺里、game_agent、规则bot的席位与授权属于modes/room/director。
- 认输目前沿通用Match规则（其余席位胜），尚未实现地主/农民阵营认输。

## 验证入口

```bash
cd plugins/nori_plugin_gamehub
../../.venv/bin/python -m pytest tests/test_engine_doudizhu.py tests/test_doudizhu_flows.py tests/test_web_api.py -q
```

规则全自动对局不是对真实模型出牌、手机浏览器或实际语音播放的验收。
