# kira-plugin-gamehub

KiraAI game center plugin: gomoku (五子棋), reversi (黑白棋), doudizhu (斗地主).

- Chat-side: play against the host AI, ask for the board, resign.
- WebUI: lobby + live match page (gomoku / reversi / doudizhu).
- Seats can be driven by the rule bot, the plugin `game_agent`, or a notice-driven turn of the host agent.

Ported from `nori_plugin_gamehub`; the `engine/` package is host-independent.

## Install

Pull the plugin into the KiraAI plugin directory and restart the service:

```bash
git clone <repo> /data/KiraAI/data/plugins/kira-plugin-gamehub
systemctl --user restart kira.service
```

The plugin then appears in the WebUI sidebar as **游戏中心 / Game Hub**.

## Layout

| Path | Purpose |
|---|---|
| `main.py` | Plugin entry: chat tools, page/API/WS registration, adapter + session provisioning |
| `engine/` | Host-independent game logic (gomoku / reversi / doudizhu) |
| `room.py` | Authoritative in-memory match state |
| `modes.py` | Pure mode / role / driver helpers (`nori` vs `bot`, `turn`/`agent`/`bot` drivers) |
| `director.py` | Notice-driven turn director, grants and watchdog |
| `game_agent.py` + `host/llm.py` | Plugin-owned LLM move loop on the host LLM client |
| `talk.py` / `notice_judge.py` | Table talk and the local key-event judge (no external judge) |
| `tools.py` / `tools_turn.py` | Chat tools and request-scoped turn tools |
| `host/` | Everything that touches the KiraAI host (store, notices, prompt, web, avatar) |
| `adapter/` | The internal `gamehub` IM adapter backing `gamehub:dm:admin` |
| `web/` | The lobby and match pages (native ESM, no build step) |

## Driving the match

- **Chat**: the user asks the host AI to start a game; the plugin answers with
  `game_new` / `game_state` / `game_resign` tools.
- **Opponent seat**:
  - `driver=turn` — the host agent is driven by an action notice and answers with
    the request-scoped `play_<match>` tool (the notice arrives in the session the
    match belongs to).
  - `driver=agent` — the plugin's `game_agent` plays with a configured LLM
    (empty `ai_models` = the host default LLM).
  - `driver=bot` — the rule bot plays.
- **Teammate seat**: the host agent may take over the human seat (delegation) and
  only comment on the game.

Two identities are kept apart: the **host agent** (the persona KiraAI runs,
e.g. Seki) plays `driver=turn` seats and speaks the table talk; the plugin's
**`game_agent`** (default `nori`) plays `driver=agent` seats with its own model.

## Configuration

All options live in the plugin config form (`schema.json`, Chinese + English).
The most relevant ones:

- `agent_name` / `agent_persona` — who the game agent is.
- `ai_models` — ordered LLM failover list (`provider:model_id`); empty = the
  host default LLM.
- `ai_mode` — `agent` (LLM) or `bot` (rules only).
- `notice_target_session_id` — where table talk and turn notices are delivered.
- `agent_avatar` — optional local avatar file; the WebUI can also upload one.

## i18n

Menus, manifest and the config form are bilingual (zh + en); the page body is
Chinese by decision. REST/WS errors carry a code that the WebUI renders in the
host language (`localStorage["language"]`).

## Tests

```bash
python -m pytest tests -q     # engine + host integration tests
cd web && node --test tests/   # front-end unit tests
```

## Docs

`docs/HOST_ADAPTER.md` describes how the plugin binds to the KiraAI host
(adapter provisioning, notices, request-scoped tools, REST/WS routes).
