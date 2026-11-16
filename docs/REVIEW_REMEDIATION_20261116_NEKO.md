# Review remediation ledger — NEKO findings (2026-11-16)

Branch `fix/review-remediation-20261116`; baseline `3788107`. This ledger records
the disposition of NEKO's first review and optional suggestions on PR #1.

Validation owner: this lane runs the focused Python tests; the parent runs the
full suite (a whole-suite run was also executed here as a sanity check, see
Verification).

## Finding 1 — legacy resignation fallback only checked the last log entry

**Accepted.** `_resignation()` resolved a missing/garbage `resigned` column by
inspecting `match.log[-1]` only. A legacy/hand-repaired snapshot whose log has
any entry *after* the `resign` entry (or several resignations) was read as
**active** — reviving a finished game, the exact failure P1-3 exists to
prevent. `apply_match_action` refuses a resign on a terminal match, so a fresh
snapshot can never grow such a tail; only legacy/corrupt rows can, which is
precisely the path this fallback serves.

Fix (`engine/match.py`):

- The log is scanned **backwards** for the newest `resign` entry
  (`_log_resign_seat` + `reversed(match.log)`). Newest-resign-wins is the
  correct precedence if a corrupt row holds more than one.
- Malformed resign seats stay fail-closed: the helper returns a `_NO_RESIGN`
  sentinel for "not a resign entry" so a resign entry whose seat is
  `None`/`True`/`"x"`/out-of-range is *found* and mapped to
  `unresolvable=True` (terminal, `winners=[]`), never skipped to an older
  entry and never used to index the seat list. `bool` is excluded from the
  seat range test (a `True` seat is not seat 1).
- An explicit valid `resigned` seat still wins over the log (unchanged).

Note: `host/store.py::_resigned_from_log` already scans backwards for the DB
column back-fill; it is a separate module and was left untouched, so the
engine-side resolver now agrees with it instead of contradicting it.

Tests (`tests/test_engine_status.py`):

- `test_legacy_resignation_is_found_by_scanning_backwards` (parametrized: one
  later move, two later moves, a newer resignation) — terminal,
  `waiting_on == []`, `result_text == "P1 认输"`, view echoes seat 1.
- `test_malformed_resign_log_entry_fails_closed` (parametrized seats
  `99, -1, True, "x", None`) — terminal, `winners == []`,
  `result_text == "对局已认输（座位数据异常）"`, view does not echo a seat.
- `test_log_without_resign_entry_keeps_active_match_active` — the scan does not
  invent a resignation from ordinary moves.

## Finding 2 — `_resign_outcome` falls back to "everyone but the resigner"

**Accepted, with the distinction the finding asks for.** The baseline treated
any non-`Outcome` return (including `None` from a raising or abstaining
override) as "use the generic rule". For doudizhu (landlord vs two farmers)
that generic rule lists the resigner's **teammate** as a winner — the exact
bug P1-4 fixed — whenever the camp hook raised or returned `None`.

Fix (`engine/match.py`), three cases distinguished by *how* the hook is
provided:

- **Inherited default** (`type(mod).outcome_for_resign is
  GameModule.outcome_for_resign`; gomoku/reversi): the generic "everyone but
  the resigner wins" rule is correct for zero-sum two-seat games and is kept
  unchanged. Two-seat semantics are untouched.
- **Game override**: a returned `Outcome` is used as-is (doudizhu's camp
  verdict is unchanged).
- **Override failure** (raises, returns `None`, or returns a non-`Outcome`):
  the verdict is unknown, so the result is a terminal outcome with
  `winners=[]` and text `认输` — the same safe shape `status_of` already uses
  for other underivable results — never a guessed winner. The failure is
  logged once per match/seat through the existing `_warn_status_once` dedup
  (the helper gained an `exc_info` keyword; no new helper was introduced —
  `core/utils` has no logging/throttling helper for this).

Duck-typed modules (no `outcome_for_resign` attribute on the type, e.g. the
`SimpleNamespace` doubles in `tests/test_agent_inputs.py`) keep the generic
rule unless an instance attribute is callable, so existing test doubles and
extension modules are unaffected.

The `engine/types.py` hook contract now documents the distinction between the
inherited default and an override that fails to produce a verdict.

Tests (`tests/test_engine_status.py`):

- `test_failed_override_hook_is_terminal_without_winners` (parametrized:
  returns `None`, raises, returns a string) — on a three-seat resignation,
  `winners == []` (never `[0, 2]`), terminal, `result_text == "A 认输"`.
- `test_override_hook_outcome_is_used_as_is` — a returned `Outcome` passes
  through.
- `test_inherited_default_hook_still_uses_the_generic_rule` — the base hook is
  identified as inherited and two-seat resignation still awards seat 0.
- Existing `test_two_seat_resign_keeps_legacy_rule`,
  `test_farmer_resign_never_wins_for_the_partner`,
  `test_landlord_resign_loses_to_both_farmers` and
  `test_resign_during_bidding_is_terminal_and_never_names_the_resigner`
  remain green unchanged.

## Finding 3 — Origin host-authority branch ignores the scheme before the configured public origin

**Partially accepted; the suggested blanket scheme check is rejected.** The
approved TLS-terminating reverse-proxy semantics (`docs/HOST_ADAPTER.md`
§"Invitation-only spectator listener", `schema.json` hint, and the P1-10 entry
in `REVIEW_REMEDIATION_20261116_RUNTIME.md`) are explicit: behind a TLS proxy
the browser sends an `https://` Origin while the listener speaks plain HTTP and
`request.scheme` cannot see that, so **authority equality is the boundary**,
scheme-agnostic. Requiring scheme equality on the Host branch would 403 every
legitimate spectator behind such a proxy — a regression, not a fix.

The genuine gap is narrower and real: when the proxy forwards the *public*
`Host` verbatim (a common `proxy_set_header Host $host` setup, in which case
`spectator_public_url` is not required), the Host branch accepted the same
authority with **either** scheme, so an `http://` Origin for a deployment
pinned to `https://` in `spectator_public_url` passed. That is a plaintext
downgrade of the CSRF boundary on the very deployment that declared its scheme.

Threat assessment: the spectator surface is token-authenticated (invitation +
HMAC-bound cookie) and `Origin` is the CSRF guard; the impact of the gap is
limited to an http-scheme origin on the same authority for a pinned https
deployment. It is a real weakening of the declared configuration, so it is
fixed — without touching the approved scheme-agnostic proxy path.

Fix (`host/spectator.py::_origin_allowed`):

- If `spectator_public_url` is configured and the Origin's **authority equals
  the configured authority**, the configured **scheme must match**; a mismatch
  is rejected (this check now runs before the Host branch, so the forwarded
  public Host can no longer bypass it).
- For any other authority, the Host branch stays exactly as approved:
  scheme-agnostic authority equality (TLS proxy with a rewritten internal Host
  keeps working).
- With no configured (or an unparseable) public origin, the Host branch is
  unchanged and scheme-agnostic.
- `spectator_public_url` remains "authority-scoped": configuring it does not
  newly forbid the direct same-Host path on a different authority.

Tests (`tests/test_spectator.py`):

- `test_origin_configured_scheme_is_authoritative_for_its_authority`
  (parametrized): configured `https://watch.example.com` + forwarded Host
  `watch.example.com` rejects `http://watch.example.com` and accepts
  `https://watch.example.com` (the regression); the same for an explicit
  `http://` public URL with an `https://` origin; and an Origin on a different
  authority that matches Host stays scheme-agnostic in both directions (the
  TLS-proxy path is preserved).
- `test_origin_unparseable_configured_url_leaves_host_rule_scheme_agnostic` —
  a broken configured URL does not change the Host branch.
- Existing `test_origin_same_authority_direct_http_is_allowed`,
  `test_origin_foreign_malformed_or_dirty_is_rejected`,
  `test_origin_configured_public_url_matches_scheme_and_authority`,
  `test_origin_malformed_configured_url_fails_closed_but_host_stays_valid`
  and `test_guard_answers_403_for_rejected_origins` remain green unchanged.

`docs/HOST_ADAPTER.md` documents the configured-authority scheme precedence.

## Frontend suggestions

- **Accepted:** chat backfill now restores `aria-live="polite"` in `finally`,
  including a DOM append failure. A behavioral regression checks both restored
  announcements and the existing inline error handling.
- **Rejected:** numeric spectator UID compatibility. `host/spectator.py::join`
  always generates `<match_id>:<hex digest>` and a string credential; there is
  no numeric producer or documented legacy numeric contract. The parser keeps
  rejecting malformed response shapes rather than adding speculative coercion.
- **Accepted with a corrected implementation:** a missing/stale radio value
  now enters at the first option on next and the last on previous. The suggested
  index-zero clamp would incorrectly skip the first option on next. Tests cover
  missing/stale values, Home/End, and single-option groups.

## Verification

Focused (this lane's validation):

```
cd data/plugins/kira-plugin-gamehub
/Users/skyzhishui/workspace/KiraAI/.venv/bin/python -m pytest \
  tests/test_engine_status.py tests/test_spectator.py -q
```

Result: **85 passed** (64 before; the 21 new parametrized cases account for the
increase).

Sanity full-suite run (owned by the parent, executed here only as a check):

```
/Users/skyzhishui/workspace/KiraAI/.venv/bin/python -m pytest tests -q
```

Result: **640 passed** in 35.67s. `git diff --check` passes for the files in
this lane. The working tree also carries uncommitted `web/` changes from other
lanes; they are untouched by this lane.
