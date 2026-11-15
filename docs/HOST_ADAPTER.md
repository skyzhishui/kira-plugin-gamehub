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
- Session sid: `gamehub:gm:room` (one shared group, not one session per match).
- `main.py::GamehubPlugin._provision_adapter()`:
  1. drops a stale adapter class of the same platform (reload safety);
  2. `await ctx.register_adapter("./adapter")` (registers the platform type);
  3. `await ctx.adapter_mgr.register_adapter(adapter_info())`;
  4. `await ensure_game_session(ctx)` →
     `ctx.db.save_chat_session("gamehub:gm:room", kind="im", title="游戏中心", data={"gamehub": True})`.
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
A notice triggers the Game Hub group router → flush → full IM pipeline.

### Notice body contract (short public event + context pointer)

Every main-AI notice is a **short public event**: the trigger facts
(situation + acting identity) plus a pointer to the per-request
`gamehub_context` block ("盘面与规则见本轮 gamehub_context 注入块；若缺失，
先调用 `state_<match>` 查看局面再行动。"). Notices persist into session
history, so they never carry rules, boards, tag syntax, examples, threats or
speech guidance — all of that rides the ephemeral block (persist=False).

- Turn notices (`director.build_turn_notice`) follow one confirmed shape:
  chess ``[System: 对局通知 · N 手：<mover> 刚落子 <move>，现在轮到你
  （<agent>，执<label>）。盘面与规则见本轮 gamehub_context 注入块；若缺失，
  先调用 state_<match> 查看局面再行动。]`` (first move / auto-pass reuse the
  same clause shape); doudizhu ``[System: 对局通知 · 斗地主第<N>局：
  <situation>，现在轮到你（…）。局势与规则见本轮 gamehub_context 注入块；…]``
  where the situation is the most recent public move/pass with its actor and
  concrete card faces (the landlord-lead/re-lead facts stay, no action
  prompts). A delegated grant folds a short ``<agent> 代 <human>`` clause
  into the turn phrase — no seat definition and no second identity sentence;
  the full identity lives in the context requirements section. The granted
  seat stays the highest-priority viewer, so a delegated seat never reads as
  a sideline teammate.
- Retry notices (`build_nudge_notice`, `note_tag_rejected`): status only +
  pointer. Errors, the move, tag syntax and examples are recorded on the
  grant (`TurnGrant.last_rejection`) and rendered by the block's requirement
  section; the "系统会替你随手一手"-style threat wording is gone (bounded
  retries + context feedback replaced it).
- Start/end stage notices (`build_stage_notice`): short event (title,
  seats, result) + pointer, **never an action authorization** at endgame.
  The event is recorded per `(session, match_id, version)` slot through
  `context.note_stage_event` before publishing — a flat bounded map with
  TTL eviction (no second state machine) — so the end-of-match context
  stays injectable even when the room has left the active set (no
  `active_rooms` dependency) and sibling matches sharing a session never
  mix. Short-term retry state (`TurnGrant.last_rejection`, the misplaced
  tag marker) is grant-scoped: per match by construction, cleared on a
  successful submission, bounded by the grant retention/prune.
- Table talk / companion notices (`talk.py`): with the shipped-default
  template the notice is one short public event line + context pointer;
  board/plan/tone guidance rides the block. A user-customised
  `table_talk_prompt_template` (identity-compared with the shipped default)
  still renders exactly as configured — its richness is a user-owned
  choice; that is the deliberate compatibility boundary. Tier gates,
  cooldowns and dedup are unchanged.

### Tag submission contract and explicit format rejections

The move travels as a `play_<match>` **root control tag**: same level as
`<msg>`, at the end of the reply, move as the tag **body**. Core
`parse_xml` silently no-ops a root play tag NESTED inside `<msg>` (it
invokes the tag handler as an in-message tag, producing no
`RootTagAction`) and parses an attribute-form tag (`<play move="pass"/>`)
into a RootTagAction with an empty body. The plugin closes both silent
paths without touching core:

- `PlayTurnTag.handle` records a fresh nested-tag attempt on the grant
  (`tag_misplaced`); `note_response` clears the marker at every new step so
  stale markers cannot misfire;
- `submit_play_from_actions` turns both cases (nested / empty body) into a
  bounded **format rejection** through `director.note_tag_rejected` (same
  budget as the text nudges) and records the specifics on
  `grant.last_rejection` for the context block;
- `director.note_response` no longer suppresses the F1 corrective based on
  the reply substring — a substring proves nothing. The authoritative
  stand-down signals are parse-level: `note_tool_progress` reported by the
  submission path (with the arming instant captured at `note_response`
  time, so the race is deterministic) and the grant resolving on success.

## Invitation-only spectator listener

Only the spectator surface runs on a separate aiohttp listener. The lobby and
player controls remain on the authenticated host WebUI. The listener exposes no
host administration, control-token, move, delegation, identity or new-game API.

- `spectator_enabled`: defaults to true.
- `spectator_host`: defaults to `0.0.0.0`; use `127.0.0.1` behind a local proxy.
- `spectator_port`: `0` derives the host's `data/webui.json` port + 10000
  (normally 15267). Invalid/occupied ports disable the spectator service with an
  error log, without taking the game runtime down.
- `spectator_public_url`: optional separate HTTPS origin. Without it, the player
  browser's hostname plus the spectator port is used over HTTP (trusted LAN).
  For public access, configure HTTPS/WSS and preserve the public Host/Origin at
  the reverse proxy. Do not expose the host WebUI through this proxy.

The player uses **Copy spectator link** to obtain a per-match random invitation.
The invitation is carried in the fragment and removed from the address bar on
entry. A guest chooses a temporary nickname and exchanges the invitation for a
cookie-bound bearer credential. UID = match id + HMAC of a random visitor cookie;
no host login cookie, browser fingerprint, JWT or player seat token is used.
Do not log query strings at a WS reverse proxy: the WS handshake uses a short-lived
spectator credential in `access`, not the invitation or host token.

Invitations and guest credentials are memory-only: they expire on match end or
plugin restart. Existing WS connections close on match end (at most one second
for an idle connection). Refreshing after a restart requires a newly copied
invitation. An individual guest credential also expires after 24 hours.

`host/group_chat.py` publishes native sender/group events. The priority-99 group
hook stops further inbound chat scoring: guests only enter a bounded buffer;
players and trusted server notices flush it. Every authenticated player message
is an unconditional strong signal, even without a mention or wake word: no
reply probability, activity score or chat-gate threshold applies. Wake words and mentions from guests
therefore cannot arm NoriEngineChat reply tasks. Host tools/capabilities remain
under normal host session permissions. The new group has its own session key:
review/copy the old `gamehub:dm:admin` tool permission settings as appropriate;
the old private-session history is retained, not deleted or silently migrated.

The AI context is intentionally shared across matches. Spectator REST history is
filtered by match id and never exposes previous matches' chat. AI group replies
are visible in currently active room windows. Guests are trusted invitees, and
invitation links can be forwarded; this is not an account or anti-Sybil system.

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

- `tools_turn.py` (plugin root) defines the tag/tool pair whose `name` is
  `play_<match_id>` (root control tag, the move channel) / `state_<match_id>`
  (native read tool).
- `director.py::TurnDirector.tools_for(sid)` returns the tool instances that must
  be visible for the current request (empty when it is not the AI's turn).
- `main.py`'s `@on.llm_request` handler calls
  `req.tool_set.add(*director.tools_for(event.sid))` and registers
  `PlayTurnTag` on the per-request `tag_set`; the host re-derives
  `request.tools` from `request.tool_set` afterwards, so the tools reach the model.
- Revocation = not adding them on the next request. The watchdog (90 s) still
  lives in `director.py` and fills with the rule bot when no legal move arrives.
- `BaseTool.execute(self, event, **args)` receives the `KiraMessageBatchEvent`
  first; return a string. To end the turn after a successful move, call
  `event.stop()` (FinalizeStage still saves memory).
- Tag rejections are bounded (`turn_text_retry_max`) and never silent: see
  "Tag submission contract and explicit format rejections" above.

## Rule-bot pacing

Rule-bot seats (`kind=bot`, the practice partner) never answer inside the same
request as the opponent's move. The engine's inline `autoplay` is disabled by the
room layer (`apply_match_action(..., autoplay_bots=False)`,
`create_match(autoplay_bots=False)`), and `room.GameRoom` schedules each bot move
as a delayed task (`_check_bot_locked` → `_spawn_bot_steps` → `_run_bot_step`),
so the move lands as a separate version bump + WS broadcast.

- Delay window: `bot_move_delay_min_ms` / `bot_move_delay_max_ms`
  (default 1000 / 2000 ms, random per move; `0 / 0` falls back to 1-2 s).
- `game_agent`'s rule-bot fill (`ai_mode=bot`, LLM failure fallback) uses the same
  window through `_wait_bot_pace`, so a bot-only match is paced too.
- Both take the move from `engine.match.bot_move`, which keeps the level-based
  difficulty perturbation that `autoplay` used to apply.

## Prompt injection

`host/prompt.py` builds the per-session board block (nori `context.py` logic) and
appends a `Prompt(name="gamehub_context", source="user", persist=False, content=...)`
to `req.user_prompt`, so the system-prompt cache prefix stays stable.

While a turn grant is live, the block additionally carries the **turn
requirement section** (`context._turn_requirements`) for the granted match:
authorization identity (grant seat first — 代打 when delegated), the granted
seat, `play_<match>` tag syntax (root level, end of reply, move as the tag
body, never an attribute), the move format with a legal **example**
(`context.example_move`, labelled format-only), feedback semantics (a
rejection produces one short notice; correct against the latest snapshot)
and the unified speech line. Doudizhu adds the private-card boundary
(never reveal unplayed cards / private strategy in public speech).

The requirement section survives every budget-degradation level of the
block (full payload → summary → minimal): the block may exceed its nominal
character budget by exactly that bounded section rather than lose identity,
format or feedback. `TurnGrant.last_rejection` adds the "上次提交未生效"
line until the next successful submission clears it. The unified speech
line is carried verbatim (`context.SPEECH_LINE`, ending at
"不要复述你的落子计划") with the media sentence (`SPEECH_MEDIA_LINE`)
appended after it.

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
