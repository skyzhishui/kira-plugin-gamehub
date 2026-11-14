# Host adapter contract (KiraAI)

This plugin keeps the nori layout for ported game logic and isolates every
KiraAI-specific dependency in `host/`.

```
kira-plugin-gamehub/
├── main.py                 # BasePlugin + @register.* wiring only
├── engine/                 # host-independent (verbatim + optional host-module shim)
├── room.py                 # in-memory authoritative match state
├── modes.py                # pure mode/role/driver helpers
├── assistance.py           # difficulty + ranked moves
├── plan.py                 # pure plan helpers
├── identity.py             # page player identity
├── agent_history.py        # per-(match, seat) transcript
├── targets.py              # sid parsing / session routing
├── talk.py                 # table talk notices
├── notice_judge.py         # LOCAL key-event rules only (System1 removed)
├── context.py              # board context text for prompts
├── tools.py                # 4 chat tools (plain functions)
├── tools_turn.py           # turn-scoped tools play_<match>/state_<match>
├── director.py             # notice-driven turn director + watchdog
├── game_agent.py           # plugin-owned LLM move loop (KiraAI client adapter)
├── host/
│   ├── bridge.py           # outbound sink + match WS hub
│   ├── session.py          # dedicated game session + adapter provisioning
│   ├── store.py            # private SQLite persistence (aiosqlite)
│   ├── notices.py          # publish_notice wrapper
│   ├── prompt.py           # @on.llm_request board-context injection
│   ├── avatar.py           # game_agent avatar (bundled default + upload)
│   ├── llm.py              # LLMRequest/tool_set adapter for game_agent
│   ├── runtime.py          # wires room/talk/director/agent/store
│   └── web.py              # REST + WS routes (P3)
└── adapter/                # plugin-owned lightweight IM adapter "gamehub"
    ├── manifest.json
    └── adapter.py
```

Import conventions:
- Plugin-root modules import engine as `from .engine ...` (production package).
- `host/*` imports plugin-root modules as `from ..room ...`.
- Engine's `_host_module("modes"|"assistance")` resolves plugin-root siblings in
  production and falls back to `None` for standalone engine tests.
- Logging: `from core.plugin import logger` everywhere. No `logging.getLogger`.

## Session & adapter

A dedicated session keeps game notices out of the user's real WebChat session.

- Adapter platform/name: `gamehub`; manifest `{"name": "gamehub", ...}`.
- Session sid: `gamehub:dm:admin` (WebUI is single-admin).
- `main.py::GamehubPlugin._provision_adapter()`:
  1. drops a stale adapter class of the same platform (reload safety);
  2. `await ctx.register_adapter("./adapter")` (registers the platform type);
  3. `await ctx.adapter_mgr.register_adapter(adapter_info())`;
  4. `await ensure_game_session(ctx)` →
     `ctx.db.save_chat_session("gamehub:dm:admin", kind="im", title="游戏中心", data={"gamehub": True})`.
- Adapter registration resolves the calling module through the call stack, so it
  must stay in `main.py` (never in `host/`).
- `terminate()` unregisters the adapter and stops it.
- The adapter's `IMCapability.send_direct_message(user_id, chain)` forwards to
  `host.bridge.emit_outbound(sid, chain)`; `receive_message` is unused (the plugin
  injects inbound notices via `publish_notice`).
- `host/bridge.py`:
  ```python
  def set_outbound_sink(fn): ...
  async def emit_outbound(sid: str, chain) -> None: ...
  class MatchWSHub: subscribe/unsubscribe/broadcast(match_id, payload)
  ws_hub = MatchWSHub()
  ```

## Notices

```python
# host/notices.py
async def send_notice(plugin, sid: str, text: str) -> bool
```
Resolves the session and calls
`ctx.publish_notice(session, MessageChain([Text(text)]), is_mentioned=True)`.
A notice triggers the normal chat plugin buffer → flush → full IM pipeline.

## Persistence

```python
# host/store.py
class MatchStore:
    def __init__(self, data_dir: Path)      # <plugin_data>/gamehub.sqlite3
    async def init(self) -> None            # create_all + column migrations
    async def upsert(self, match) -> None   # snapshot row, last-write-wins
    async def load_active(self, limit: int) -> list[Match]
    async def archive_stale(self, days: int) -> int
    async def close(self) -> None
```
Use `sqlalchemy.ext.asyncio.create_async_engine("sqlite+aiosqlite:///...")` with a
plugin-private `DeclarativeBase` (same `gamehub_matches` columns as nori).

## Turn-scoped tools

KiraAI has no `ctx.session_tools`; grants are request-scoped:

- `tools_turn.py` (plugin root) defines the `BaseTool` subclasses whose `name` is
  `play_<match_id>` / `state_<match_id>`.
- `director.py::TurnDirector.tools_for(sid)` returns the tool instances that must
  be visible for the current request (empty when it is not the AI's turn).
- `main.py`'s `@on.llm_request` handler calls
  `req.tool_set.add(*director.tools_for(event.sid))`; the host re-derives
  `request.tools` from `request.tool_set` afterwards, so the tools reach the model.
- Revocation = not adding them on the next request. The watchdog (90 s) still
  lives in `director.py` and fills with the rule bot when no legal move arrives.
- `BaseTool.execute(self, event, **args)` receives the `KiraMessageBatchEvent`
  first; return a string. To end the turn after a successful move, call
  `event.stop()` (FinalizeStage still saves memory).

## Prompt injection

`host/prompt.py` builds the per-session board block (nori `context.py` logic) and
appends a `Prompt(name="gamehub_context", source="user", persist=False, content=...)`
to `req.user_prompt`, so the system-prompt cache prefix stays stable.

## Game agent

`host/llm.py` adapts the host LLM client to the nori `run_replyer_stream` shape:
- build `LLMRequest(messages=..., tool_set=ToolSet([PlayTool(...)]))`
- `client = ctx.get_llm_client(model_uuid)` with ordered failover over config
  `ai_models` (values are `provider:model_id`)
- use `client.chat(request)` for a complete response (`LLMResponse.tool_calls`);
  streaming is optional and only needed for perceived latency.
- `game_agent.py` keeps nori's rounds/timeout/concurrency/fallback semantics.

## Identity split (KiraAI)

Two identities coexist and must not be conflated:

- **Host agent** (e.g. Seki) — plays `driver=turn` seats and speaks the table
  talk. Its display name comes from the active persona
  (`ctx.persona_mgr.get_active_persona()`), exposed as `plugin.host_agent_name`
  and `ui_config.host_agent_name`.
- **`game_agent`** (default `nori`) — plays `driver=agent` seats with its own
  LLM loop; configured by `agent_name` / `agent_persona` / `ai_models`.

Consequences: turn notices and talk prompts self-reference the host agent;
`game_agent` prompts use the plugin persona; seat names follow the driver
(`turn` → host name, `agent` → `agent_name`, `bot` → `practice_name`).

## i18n

- Menus, manifest and the whole config schema are bilingual (zh + en).
- The page body is Chinese (recorded exception).
- REST/WS failures carry a stable `code`; the WebUI maps it to zh/en through
  `web/shared/i18n.js` (`localStorage["language"]` selects the language).

## WebUI contract

- Page: `main.py` registers `@register.page("/hub", ...)` →
  `PluginPage.from_folder("./web")`. The host serves it at
  `/page/plugin/kira-plugin-gamehub/hub/` (StaticFiles, `html=True`).
- REST: `main.py` registers thin `@register.api` wrappers that delegate to
  `host/web.py::GamehubApi`; the envelope is `{"ok": bool, "data": ..., "error": str}`
  and failures carry an HTTP-like status (`ApiError.status`).
  Routes: `/ping`, `/page-session`, `/identity` (GET/POST), `/ui-config`,
  `/notice-test`, `/games`, `/matches` (POST), `/matches/{id}` (GET),
  `/matches/{id}/control`, `/matches/{id}/actions` (POST),
  `/matches/{id}/mode` (GET/POST), `/matches/{id}/delegation` (POST),
  `/agent-avatar` (GET/POST).
- WS: `@register.ws("/matches/{match_id}/ws")` → `host/web.py::serve_ws`.
  The host calls the handler with the socket only, so the match id is read from
  `ws.path_params` and the seat token from `?t=`; the host JWT is read by the
  host's own `require_ws_auth` dependency (`?token=` or `Authorization`).
- Auth: the front end reads the host JWT from `localStorage["jwt_token"]` and
  sends `Authorization: Bearer <jwt>`; `<img>`/`<audio>`/WS fall back to the
  `kira_token` cookie or the `?token=` query.

## Game agent avatar

`host/avatar.py` owns the only character image of the plugin:

- default: the bundled `web/assets/agent-avatar.png` (nori's avatar);
- override: an upload stored at `<plugin_data>/agent_avatar.<ext>`, served by
  `GET /api/plugin/kira-plugin-gamehub/agent-avatar`;
- `POST /agent-avatar` takes `{"data": "data:image/png;base64,..."}` to upload
  or `{"reset": true}` to fall back to the bundled default.

## Naming

- Match ids: nori `match_id` format is preserved (`g-...`, etc.).
- Tool names: `play_<match_id>`, `state_<match_id>`, plus global
  `game_list` / `game_new` / `game_state` / `game_resign`.
- Table-talk tiers: `judge` = local key-event rules only (no System1),
  `moves` = every move with cooldown, `key` = every N moves + start/end,
  `off` = silent.
