# Review remediation — 2026-11-16

Source: `kira-plugin-gamehub-review-20261116.md`, baseline `93f7eba`.
Host contract baseline: KiraAI `v3.2.0-beta.1`, not the older local host checkout.
Changes are on the review-remediation branch, including the completed test-suite simplification pass.

## P1 disposition

| ID | Resolution | Main regression evidence |
| --- | --- | --- |
| P1-1 | Ordinary tools use the caller's actor/session seat; unseated doudizhu viewers get public-only summaries. Bound tools retain their authorized grant perspective. | `test_tools.py`, `test_game_tools_roles.py` |
| P1-2 | By-id resignation requires match-seat ownership, an actual host AI seat, and cancels drive tasks after applying. Delivery targets alone do not authorize it. | `test_tools.py` |
| P1-3 | Validate serialized containers, seat counts and game state shapes; reject malformed snapshots without remapping seats. Invalid resignation stays terminal instead of indexing names or reviving the match. | `test_engine_status.py`, repository tests |
| P1-4 | Game-specific resignation hook; a farmer's resignation awards victory only to the landlord. | `test_engine_status.py` |
| P1-5 | Explicit threat-analysis opt-out takes precedence over difficulty policy. | `test_agent_inputs.py` |
| P1-6 | Inject room bot name and update existing rooms when the host name changes. | `test_room.py` |
| P1-7 | Doudizhu table notices distinguish start, ordinary action and terminal result. | `test_talk.py` |
| P1-8 | Sanitize authorization-seat names and board-header seat names. | `test_chat_context.py` |
| P1-9 | Stop producers, flush rooms, close room sockets, close store last; partial teardown failures remain isolated. | `test_runtime.py`, `test_web_api.py` |
| P1-10 | Strict Origin parsing, scheme-independent same-authority proxy access, explicit configured public-origin fallback; malformed inputs fail closed. | `test_spectator.py` |
| P1-11 | Bilingual model hints now identify the host-default LLM fallback. | `test_config_contract.py` |
| P1-12 | Remove fictional identity implementation; preserve the live nickname-length constant. | `test_talk.py`, web API tests |
| P1-13 | Delete dead judge helpers, retain live key-event extraction. | `test_notice_judge.py` |
| P1-14 | Independent bound-state description matches tag-based action submission and grant-scoped read-only state. | `test_tool_schema.py` |
| P1-15 | Remove unwired media/CSS/SSE scaffolding and unused exports; keep the live empty media container. | web bubble/chrome tests |
| P1-16 | Create the test data directory before host imports. | `tests/conftest.py` |

## Additional corrections

- Engine: preserve doudizhu logs on resignation; use `replace` for snapshot bumps; share board color helpers; normalize invalid persisted lineups on read; repair state-analysis shape and stale game comments.
- Tools: shared tag-name sanitizer, one private-session predicate, remove fake identity hooks and obsolete MOVE filtering; doudizhu explicitly rejects unsupported difficulty and respects a requested bot lineup.
- Runtime: fail-closed grant visibility, delivered-notice retry accounting, director task cancellation/error observation, shared bot delay parsing and history normalization, bounded decision logs, avatar pre-decode limits, UTF-8 spectator assets, stop-aware WS closure, and serialized lazy-store initialization with readiness-barrier tests.
- Frontend: shared turn-seat detection, visible paused chip, keyboard focus preservation, lineup radiogroup navigation, spectator-envelope validation, quieter chat backfill, corrected token/footer and column comments, CRLF-independent tests.
- Test/dead-code cleanup: remove obsolete identity/media tests with their deleted implementations, add behavior tests for live frontend helpers, derive host test roots rather than assuming installation depth. Frontend test totals therefore decrease despite new regressions.
- Adapter metadata now advertises only text, matching the actual outbound renderer rather than enabling unsupported host media tags. Direct assistance tests and real-SQLite legacy migration tests cover column addition, resignation precedence, chat defaults and reopen/idempotence.

Runtime details, evidence and further deferrals: [runtime ledger](REVIEW_REMEDIATION_20261116_RUNTIME.md).

## Report correction

The report claims `plugin.stopping` is never assigned. This is false on the baseline: `runtime.stop()` sets it. The actual defect was a WS handler blocked in `receive()`; shutdown now closes its room socket to unblock it. Do not delete the live stop check.

## Explicitly not resolved

- Product decisions: public `SeatInfo.plan` exposure and real legal doudizhu moves in format examples remain unchanged. A lane's unrequested example-policy change was reverted during integration.
- Larger structural deduplication remains: D-3/D-5/D-6/D-7/D-8/D-9/D-10/D-11 (partial)/D-15/D-16 (partial)/D-18 (partial)/D-21/D-22/D-23/D-24. In particular, room bot-step merging and canvas BoardBase extraction are not justified as correctness hotfixes.
- The game-agent `max_say_len=0` policy still differs from the turn tool. Full consolidation is not claimed.
- T-5/T-6 are not fully addressed: some weak/source-text assertions and direct talk-dispatch tests remain. T-7's brittle whole-source `httpx` exclusion check was removed in the test-simplification pass; local key-event behavior remains covered.
- `notice_judge` key/judge mode distinction needs a product decision; no new semantics were invented.
- Visual validation covers offline page loading and Chrome helper smoke checks, not a running backend or a complete online game.

## Integration verification

- Final Python full suite: **658 passed, 1 skipped** in 35.58s using `/Users/skyzhishui/workspace/KiraAI/.venv/bin/python -m pytest tests -q`, without external shims or bypassing conftest. This supersedes the earlier 634-pass integration snapshot, before all additional test files were present.
- Node full suite: **237 passed**, Node v26.9.0. Node reports an experimental localStorage warning; no test failures.
- The initial Python skip was the optional host prompt-layer injection test. The simplification pass replaced it with a runnable plugin injection test using a minimal host Prompt boundary stub; this tests plugin wiring, not compatibility with a live host provider.
- Chrome screenshots were opened and checked: paused chip visible, lobby/match offline states rendered. Full live-game UI and Windows execution remain unverified.
- Additional adapter/assistance/migration focused check: **30 passed**. Final pre-submission `git diff --check` passes.
- Independent security/correctness review gate: **accepted, no blockers** in tools/grants, shutdown, lazy-store readiness, Origin and director scope. The reviewer appended its evidence to the original report; engine/frontend were not independently deep-reviewed in this gate.
- Non-blocking follow-ups from that gate: possible cross-instance schema initialization during plugin load, same-authority Origin reliance on request Host (token authentication remains required), stale bound-event comments, fail-closed grant-cache eviction behavior, and missing-director rejection logging. None is claimed fixed here.

## Test simplification follow-up

Only tests changed in this follow-up; production behavior is unchanged. Removed redundant mirrors of host implementations, deleted-symbol/file-existence assertions, repeated schema/helper cases and source-text/comment checks with no behavior guarantee. Consolidated real-SQLite migration tests into one upgrade/load/write/chat/reopen lifecycle test plus independent resignation-precedence coverage. Parameterized repeated board and authorization matrices without removing their distinct boundary inputs.

Retained regression claims: own-seat private hands and outsider isolation, by-id resign ownership, stale bound grants, malformed snapshot rejection without seat remapping, terminal persistence, doudizhu camp resignation, hard analysis opt-out, shutdown WS release, two-phase DDL readiness, strict Origin rejection, focus memo and radiogroup state, bubble ordering and game DOM behavior.

The chat backfill aria-live check now observes actual DOM operations instead of matching source text. Prompt injection runs against the real plugin injector with a minimal host container stub, including other-session isolation; temporary module imports are restored after the test.

Across 31 edited test files: **12,602 → 11,865 lines (−737)**. Counts are secondary to retained behavior coverage; parameterized cases remain separate where that improves failure diagnosis. Final validation: **619 Python tests passed, no skips** (previously 658 passed and 1 skipped), **232 frontend tests passed** (previously 237), and `git diff --check` passes. The full Python run took 35.69s: this pass improves maintenance cost, not a claimed runtime speedup.
