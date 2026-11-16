# Review remediation ledger — runtime lane (2026-11-16)

## ERRATUM to the review report (runtime stop)

The report's §六 P2 entry for `host/web.py` states: *"WS 循环阻塞在 receive
上、stopping 只在收到新帧时可见……且 `plugin.stopping` 在插件与宿主 tag 上
均无人赋值，该停止检查实为恒 False 死代码"* (translation: "`plugin.stopping`
is never assigned anywhere, so the stop check is dead code that is always
False").

**That claim is false.** `host/runtime.py::GamehubRuntime.stop()` assigns
`plugin.stopping = True` as its first action (runtime.py line ~149, present
on the reviewed baseline). The `plugin.stopping` check in `serve_ws` was
therefore live, and it is preserved unchanged. The real defect was narrower:
a WS handler parked in `receive()` could not observe the flag until a client
frame arrived. Remediation (P1-9) keeps the check verbatim and adds
`close_all()` to the stop path so the server-side close releases the parked
`receive()` immediately — see the stop-order contract in the P1-9 entry and
the focused regression `test_stop_closing_the_room_releases_the_serve_ws_loop`
(tests/test_web_api.py).

Lane scope: `room.py`, `director.py`, `talk.py`, `notice_judge.py`,
`game_agent.py`, `agent_history.py`, `main.py`, `identity.py`, `host/**`,
`schema.json`, `manifest.json`, `README.md`, `docs/**` and the tests for
these modules + `tests/conftest.py`. Engine/modes/plan, tools/context/targets
and web/ belong to other lanes. No commits; host API contracts were judged
against the manifest's target tag (v3.2.0-beta.1), not the local checkout.

## Fixed

P1 (owned subset):

- **P1-6** — `GameRoom` now carries `bot_name` (constructor + `_new_room`
  injection); `RoomManager.set_bot_name` syncs every live room. Non-ai-seat
  error paths return `RoomError` / `(None, msg)` instead of raising
  AttributeError.
- **P1-7** — doudizhu table-talk events render per stage (start → 「对局开始」,
  end → result, resign → named resigner, else → 「完成公开动作」); the
  unreachable `0 <= mover < 3` guard is gone.
- **P1-9** — `runtime.stop()` follows stopping → producers (director/talk/
  judge + every room's drive tasks) → `flush_all()` → per-room `close_all()`
  (CLOSE_STOPPING=4003) → `store.close()`, each step exception-isolated and
  safe on a partially started runtime. The server-side close unblocks the
  page WS handlers parked in `receive()`.
- **P1-9 follow-up (documented, no code change)** — `cancel_drive_tasks()`
  fires `.cancel()` without awaiting. Analysed: every drive-task body
  (`_run_drive`, `_run_bot_step`, `game_agent.run`) re-raises
  CancelledError, `_persist_locked` catches only `Exception` (cancellation
  propagates, never swallowed mid-write), and stop's `flush_all`/`close_all`
  awaits give cancelled tasks scheduling points while the store is still
  open — a save already handed to aiosqlite serializes before the engine
  closes. A late REST-spawned bot task that wakes after `store.close()`
  hits the caught dirty-path warning (memory stays authoritative); the
  sqlite write itself cannot execute on a closed engine. **No
  writes-after-close is possible, so the fire-and-forget cancel stands**;
  the reasoning is recorded as a comment at the stop call site.
- **P1-10** — spectator Origin guard: malformed/foreign origins 403; strict
  same-authority comparison against the listener Host (no forwarded headers
  trusted); `spectator_public_url` whitelists the public origin when the
  proxy rewrites Host. Schema hint + docs updated.
  *Hardening after parent re-review of the first fix*: `_parse_origin`
  catches `urlsplit` ValueError (malformed IPv6 such as `http://[::1` used
  to escape the guard middleware as a 500 — the middleware's try only wraps
  the handler) and validates `.port`; rejects userinfo and any
  path/query/fragment (Origin syntax); the same-authority branch stays
  scheme-agnostic (approved TLS-proxy deviation); the configured public
  origin now matches **configured scheme + authority** (previously netloc
  only) and a configured base URL with a path is accepted by extracting its
  origin, while an unparseable/invalid-port configured URL fails closed.
- **P1-11** — `ai_models` schema hints now say "empty = host default LLM".
- **P1-12** — identity.py reduced to `MAX_NICKNAME_LEN`; the fictional
  mapping system and its obsolete tests (test_talk identity block) removed.
- **P1-13** — only the dead System1 helpers (`_label_of`, `_module_chat_context`,
  `_opponent_seat`, `_state_text`) deleted; live `_module_key_events` kept.
- **P1-16** — `tests/conftest.py` creates the untracked `data/` dir before any
  host import; verified by deleting `data/` and re-running the tests (see
  Verification: whole-suite runs are the parent's post-merge step, not this
  lane's gate).

Confirmed P2 (owned subset): notice_judge empty `notice_judge_mode` no longer
fail-opens to `key`; judge + decision jsonl logs rotate once past 2 MiB and
appends are serialized; talk `_last_target` pruned with the seen-TTL; talk
event attribution unified on `modes.last_mover` (no unguarded `int(seat)`);
director `_finish` cancels `turn_task`; `_tools_grant` is fail-closed;
`tag_retries` counts only delivered notices; director `on_event` logs task
exceptions instead of leaking unretrieved-task warnings; game_agent guards a
broken `ai_timeout` and dropped the 5 never-read `_Snapshot` fields;
`GamehubApi.chat_store` is race-guarded (lazy-init lock) and disposed on
terminate — *hardened after parent review of the first fix: the lock now
covers construction AND initialization, and the method returns only after
init completes. The first shape locked only the constructor, so a second
caller could grab the early-assigned ``engine`` attribute while the first
caller's DDL was still running and query before the schema existed; the
runtime store's fallback init shares the same lock/recheck, and dispose
serializes against an in-flight init. Regression uses a two-phase fake
store (engine assigned, then DDL blocked on an event) to prove the second
caller stays pending until init completes and init runs exactly once —
verified to fail against the old shape (mutation-checked).* spectator `read_text` gets `encoding="utf-8"` (×2) and per-frame
WS checks use a DB-free fast path; avatar size is bounded before base64
decode; `ping` uses the standard envelope; dead `_agent_persona` attribute
removed; priority 99 is a named, commented constant.

C/X/D/T (owned subset): C-6 (flush/close now actually wired), C-8 (dead
`agent_avatar` field removed from schema + defaults, settings docstring
honest), C-9 (schema hints + web docstring name `gamehub:gm:room`), C-10,
C-17/C-18/C-19 (room docstrings), C-24 (store column comments, comments
only), C-27/C-28 (talk comments), C-29 (resign attribution fixed; ordering
kept outcome-first — see "corrected"), C-33/C-34 (judge comments), C-36
(docs route list), C-37 (docs logging claim), C-40 (manifest repo →
`nori-home/`), C-41 + P1-16 note (README `node --test`), X-4 (`last_plan_ts`),
X-5 (dead opponent check), X-7 (`tool_name` documented compat-only), X-8
(identity dead code + dead default-identity copies in game_agent/main),
D-2 (one delay parser in room; explicit `0/0` disables the pause; `hi<lo`
clamps), D-4 (canonical room error texts + `is_stale/gone_move_error`
helpers consumed by game_agent and director — no signature change),
D-11 partial (`chat_store` public; send paths intentionally not merged),
D-14 (`ctx.bot_config` candidate dropped in talk/web + the 3 test stubs),
D-16 partial (dead 4th copy removed; schema/settings mirrors documented),
D-17 (one `normalize_tool_calls` in agent_history), D-20 (watchdog fill and
game_agent fallback go through `engine.match.bot_move`, matching the docs),
T-3 (bot_config stubs removed), T-8 (repo-root derivation in
test_main_turn_hooks and test_spectator), plus test_room's FakeRepo aligned
with the engine's new from_dict validation. New focused regressions cover
each fixed behavior (`tests/test_config_contract.py` guards the doc/data
contracts).

## Refuted / corrected (oracle)

- See the ERRATUM section above: "plugin.stopping never assigned" is false —
  `runtime.stop()` sets it; the web stop check is preserved and the receive
  unblock is delivered by close_all in the stop order.
- C-29: the resign branch stays *after* the outcome branch so terminal events
  keep carrying the result text (behaviour asserted by existing tests); only
  the attribution bug (hardcoded human name) was fixed for the defensive path.

## Deferred (with reasons)

- **naming.py integration (sequential)**: the tools lane's new leaf module
  `naming.py` (`safe_id` / `play_tool_name` / `state_tool_name` /
  `host_agent_name` / `NEUTRAL_HOST_NAME`) is live in tools.py/context.py/
  tools_turn.py only. No runtime-lane file imports it yet; when the parent
  sequences the switch, the runtime consumers are: `director` (play/state
  tool names via tools_turn re-exports), `talk._bot_nickname` and
  `host.web._bot_name/_host_agent_name` (host-name resolution), and the
  room error-text constants stay room-owned.
- **P1-3 family / store-side corruption validation** (`Match.from_dict`
  shape, `status_of` range, `row_to_match` element shapes, `_resigned_of`):
  engine lane's exclusive responsibility; store semantics untouched here.
- **D-3** host-name resolver convergence ×4 — tools/context halves are not
  owned; the owned halves (talk/director) are already bot_config-free.
- **D-5** tools/web create-validation drift — needs the tools lane.
- **D-15** neutral-fallback unification — spans engine/tools (not owned).
- **D-18** `max_say_len` zero-semantics unification — tools_turn not owned;
  local constant documented instead.
- **D-21** `ai_step` / `_run_bot_step` merge — concurrency-sensitive room
  code; the extraction risk outweighs the cleanup in this pass.
- **T-6** talk._dispatch tests bypass the production dispatch chain — kept
  semantics; a production-chain harness is a follow-up.
- **T-9** assistance.py has no direct tests (file not owned); bridge got a
  direct test; store column-migration coverage left to the engine/store lane.
- **adapter `_SUPPORTED_ELEMENTS`** overstatement — `adapter/` not owned.
- **notice_judge `key` vs `judge` tiers identical** — product semantics,
  owner decision required.
- Web-lane items (X-2/X-9, three-seat drift, hero-banner T-1, C-35, C-39…)
  and tools-lane items (C-1/C-3/P1-1/P1-2/P1-8/P1-14/P1-15, C-25/C-30/C-31/
  C-32, D-1/D-9/D-12) are other lanes' scope and were not touched.

## Verification & limits

- This lane's gate is the **focused owned-scope set only**; the whole suite
  is the parent's post-merge step. Exact final gate (run on the final tree,
  after the English comment sweep and the P1-10 origin hardening):

  ```
  python -m pytest tests/test_room.py tests/test_talk.py tests/test_notice_judge.py \
    tests/test_game_agent.py tests/test_agent_history.py tests/test_game_agent_prompt_split.py \
    tests/test_game_agent_group_chat.py tests/test_turn_director.py tests/test_turn_notice.py \
    tests/test_turn_tag.py tests/test_runtime.py tests/test_web_api.py tests/test_repository.py \
    tests/test_notice_target.py tests/test_main_turn_hooks.py tests/test_spectator.py \
    tests/test_config_contract.py -q
  → 377 passed, 0 failed (final tree, incl. the chat_store readiness
    hardening; some runs surface one intermittent warning without affecting
    the result — not attributable to this lane's code)
  ```

  The tools lane's `test_tool_schema.py` / `test_tools.py` /
  `test_game_tools_roles.py` are excluded (not this lane's files).
- An earlier intermediate whole-suite observation (611 passed, 1 skipped)
  predates the origin hardening and the comment sweep and is **superseded**;
  it does not verify the final tree. The whole suite must be re-run by the
  parent after all lanes land.
- P1-16 was verified by deleting `data/` and re-running the tests: without
  the fix the run reproduced the reported collection errors; with the fix
  it passed.
- Environment note: the checkout's `.venv` base interpreter was missing and
  was restored via uv; `greenlet`/`httpx`/`deprecated` were added to the venv
  to run the tests (env repair, no repo change).
- Shared-worktree caveat: engine-lane edits landed mid-verification; at one
  point `engine/match.py` transiently referenced an undefined
  `_module_or_none` (their file, their fix). Focused results above were
  taken on a coherent tree.
- Front-end `node --test` not run (web lane). No commits made; nothing from
  this lane is staged (the only staged entries are web-lane deletions).
