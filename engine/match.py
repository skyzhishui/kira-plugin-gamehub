"""对局层：Match 容器与统一动作（移植自 rain-go ``match/match.ts``）。

与 rain-go 的有意偏离（设计 §5.2）：

- ``describe_match`` 尾部不再输出 MCP 工具指令（``Call play.`` /
  ``Call wait_for_opponent.``），也不再输出已废弃的 ``MOVE: <走子>``
  文本协议（走子统一由 ``play`` 工具提交）；
- 可选 ``page_url`` 供页面侧对局在提示里带上入口；
- 座位可携带 ``plan``（M2 写入，M1 仅透传/持久化）。

所有函数纯函数式：入参 Match 不被修改，返回新 Match 或错误。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from . import games
from .games.doudizhu.cards import parse_cards
from .types import GameModule, MoveResultErr, Outcome, Seat, SeatKind

MAX_CHAT_LENGTH = 280
MAX_NAME_LENGTH = 40
MAX_LOG = 500
MAX_CHAT = 100

# 开局来源（显式记录在 human 座位上，随 seats_json 一起持久化）：
# - chat：聊天侧 ``game_new`` 开局，通知回开局所在会话；
# - webui：页面/WebUI 开局，通知走插件配置的默认/override 目标。
# 旧数据没有该字段（空串），读取时按 human 座位 session_id 兼容推断。
SOURCE_CHAT = "chat"
SOURCE_WEBUI = "webui"

# 陪练座位默认显示名（可配置；同一局多个陪练时第二位起加序号）。
DEFAULT_PRACTICE_NAME = "小白"

# Engine-layer logger: standalone stdlib logger (the engine must stay importable
# without the host). Bad persisted snapshots are logged, never silently accepted.
logger = logging.getLogger(__name__)

# Status boundary: invalid resigned seats become a terminal marker rather than
# indexing player names or reviving a finished match as active.
_RESIGNED_BAD = object()

# Canonical persisted marker for "somebody resigned, but the recorded seat was
# corrupt". It is an int so it survives the DB integer column and a reload, and
# it is out of range so ``status_of`` keeps treating the match as terminal.
# ``-2`` is used (not ``-1``): SQLite happily stores negative ints, and a
# smaller sentinel keeps it out of every ``0 <= seat < len(seats)`` test.
RESIGNED_INVALID: Seat = -2

# Status derivation runs on every view/broadcast; a corrupt row must not flood
# the log with one warning per read. Keys are bounded (ids of bad rows only).
_warned_status_keys: set[str] = set()

# Sentinel for ``_log_resign_seat``: distinguishes "not a resign entry" from a
# resign entry whose persisted seat is ``None``/garbage (which must fail closed).
_NO_RESIGN = object()


def _warn_status_once(
    key: str, message: str, *args: Any, exc_info: bool = False
) -> None:
    """Warn about a corrupt snapshot once per key, not once per read."""
    if key in _warned_status_keys:
        return
    if len(_warned_status_keys) >= 256:
        _warned_status_keys.clear()
    _warned_status_keys.add(key)
    logger.warning(message, *args, exc_info=exc_info)


def _module_or_none(kind: str) -> GameModule | None:
    """Resolve a game module without raising (unknown/unregistered kind).

    ``get`` is the canonical entry point; unknown kinds raise ``KeyError`` there,
    so the lookup is wrapped. Test doubles that only expose part of the registry
    API therefore keep working, and a removed/renamed game degrades to a
    terminal status instead of a 500 on every read.
    """
    try:
        return games.get(kind)
    except Exception:  # noqa: BLE001 - unknown kind -> terminal status
        return None


def _probe_module(
    module: GameModule, seats: list[SeatInfo], state: Any, match_id: Any
) -> str:
    """Probe a persisted snapshot against its game module; "" = accepted.

    Two independent checks, both minimal shape only:

    - seat count must fit the module's declared ``players`` range (a truncated
      or padded seat list would silently change which seat owns a token);
    - the module's optional ``validate_state`` hook must accept the state.

    Returns a short reason string for the caller to raise; a raising probe is
    treated as a rejection (never allowed to escape as a 500).
    """
    players = getattr(module, "players", None)
    minimum = int(getattr(players, "min", 0) or 0)
    maximum = int(getattr(players, "max", 0) or 0)
    if maximum >= minimum and not (minimum <= len(seats) <= maximum):
        return (
            f"persisted match seat count {len(seats)} not in "
            f"{minimum}-{maximum} for {module.kind}"
        )
    try:
        problem = module.validate_state(state)
    except Exception:  # noqa: BLE001 - probe must never escape as a 500
        logger.warning(
            "对局 %s 的 state 形状探测失败，按坏行拒绝",
            match_id,
            exc_info=True,
        )
        return "persisted match state probe failed"
    return str(problem or "")


def _clean_resigned(value: Any, seat_count: int) -> Seat | None:
    """Validate a persisted ``resigned`` value at the status boundary.

    Returns the seat index when it is a real seat, ``None`` when the match is
    not resigned, and the ``_RESIGNED_BAD`` sentinel when the value is garbage
    (wrong type or out of range). Callers must not treat ``_RESIGNED_BAD`` as
    "not resigned": reviving a match whose row says it was resigned would
    resurrect it as active, so it is resolved to a safe terminal outcome
    instead of ``None``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return _RESIGNED_BAD
    if isinstance(value, int):
        if 0 <= value < seat_count:
            return value
        return _RESIGNED_BAD
    # A numeric string is what a loose column reader can hand over; only an
    # exact integer spelling is accepted (never floats / free text).
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        try:
            seat = int(value.strip())
        except (TypeError, ValueError):  # pragma: no cover - guarded by isdigit
            return _RESIGNED_BAD
        return seat if 0 <= seat < seat_count else _RESIGNED_BAD
    return _RESIGNED_BAD


def _log_resign_seat(entry: Any) -> Any:
    """Seat recorded by a ``resign`` log entry; ``_NO_RESIGN`` for other entries.

    The seat is returned unvalidated: a resign entry whose seat is garbage
    (including ``None``) must still be *found* by the backward scan so the match
    stays terminal instead of being skipped as if no resignation was logged.
    Range checking is the caller's job.
    """
    if not isinstance(entry, LogEntry):
        return _NO_RESIGN
    if str(entry.move or "") != "resign":
        return _NO_RESIGN
    return entry.seat


def _resignation(match: Match) -> tuple[Seat | None, bool]:
    """Resolve a match's resignation to ``(seat, unresolvable)``.

    - ``(None, False)``: the match was not resigned.
    - ``(seat, False)``: a real seat resigned.
    - ``(None, True)``: the snapshot says resigned but no valid seat can be
      resolved (legacy rows where the column was lost, or garbage values).

    The explicit ``resigned`` field wins when it names a real seat. Otherwise
    (``None`` or a garbage value such as an out-of-range index) the log is the
    fallback — recovery is always preferred to giving up. Only when nothing
    usable is found is the match reported as unresolvable, which ``status_of``
    turns into a safe terminal outcome: reviving it as active would resurrect a
    finished game.

    The log is scanned **backwards** for the last ``resign`` entry, not just
    the tail. ``apply_match_action`` refuses a resign on a terminal match, so a
    fresh snapshot never has entries after a resignation; but a legacy/hand-
    repaired row can, and a tail-only check would then read it as active.
    Scanning for the newest resign entry is also the correct precedence if a
    corrupt row ever holds several (the last one ends the match). A found entry
    whose seat is not a real seat index stays ``unresolvable`` (fail closed)
    rather than being skipped to an older entry.
    """
    cleaned = _clean_resigned(match.resigned, len(match.seats))
    if cleaned is not None and cleaned is not _RESIGNED_BAD:
        return cleaned, False
    says_resigned = cleaned is _RESIGNED_BAD
    for entry in reversed(match.log):
        seat = _log_resign_seat(entry)
        if seat is _NO_RESIGN:
            continue
        if isinstance(seat, int) and not isinstance(seat, bool) and 0 <= seat < len(match.seats):
            return seat, False
        return None, True
    return (None, True) if says_resigned else (None, False)


def _resign_outcome(match: Match, mod: GameModule, seat: Seat) -> Outcome:
    """Outcome of a resignation (game-aware, so team semantics are right).

    Three cases, distinguished by *how* the module's optional
    ``outcome_for_resign`` hook is provided:

    - **Game override** (``type(mod).outcome_for_resign`` is not the inherited
      ``GameModule`` default): the game owns faction semantics (doudizhu:
      landlord vs two farmers) and a returned ``Outcome`` is used as-is.
    - **Inherited default** (modules such as the two-seat turn games): the
      generic rule "everyone but the resigner wins" is correct for a zero-sum
      two-player game and is kept unchanged.
    - **Hook failure** (an override raising, returning ``None``, or returning a
      non-``Outcome``): the verdict is unknown, so no seat may be declared a
      winner. Returning the generic rule here would hand doudizhu's *teammate*
      a false win, which is exactly the bug P1-4 fixed. The result is therefore
      a terminal outcome with ``winners=[]`` — the same safe shape
      ``status_of`` uses for other underivable results — never a guessed
      winner.
    """
    declared = getattr(type(mod), "outcome_for_resign", None)
    if declared is None:
        # Duck-typed test/extension module: only an instance hook counts as an
        # override; no hook at all keeps the generic two-seat rule.
        inherited = not callable(getattr(mod, "outcome_for_resign", None))
    else:
        inherited = declared is GameModule.outcome_for_resign
    if inherited:
        winners = [i for i in range(len(match.seats)) if i != seat]
        return Outcome(winners=winners, text="认输")
    try:
        outcome = mod.outcome_for_resign(match.state, seat)
    except Exception:  # noqa: BLE001 - a bad hook must not 500 the status read
        _warn_status_once(
            f"resign:{match.id}:{seat}",
            "对局 %s 的认输裁决失败，按终局无胜者处理",
            match.id,
            exc_info=True,
        )
        return Outcome(winners=[], text="认输")
    if isinstance(outcome, Outcome):
        return outcome
    if outcome is not None:
        _warn_status_once(
            f"resign:{match.id}:{seat}",
            "对局 %s 的认输裁决返回了非法结果（%r），按终局无胜者处理",
            match.id,
            outcome,
        )
        return Outcome(winners=[], text="认输")
    # An override that returns ``None`` did not judge the hand: the verdict is
    # unknown, so the fail-closed no-winner shape applies (not the generic rule,
    # which could still declare a teammate a winner).
    _warn_status_once(
        f"resign:{match.id}:{seat}",
        "对局 %s 的认输裁决没有给出结果，按终局无胜者处理",
        match.id,
    )
    return Outcome(winners=[], text="认输")


def _host_module(name: str) -> Any | None:
    """Resolve an optional host-layer module that lives beside ``engine``.

    The engine is importable both as top-level ``engine`` (standalone tests)
    and as ``<plugin>.engine`` (KiraAI host packaging), so the module is
    looked up both as a sibling of the package and as a top-level module.
    Returns ``None`` when the host layer is absent; errors raised *inside*
    an existing host module still propagate.
    """
    parent = (__package__ or "").rpartition(".")[0]
    candidates = [f"{parent}.{name}"] if parent else []
    candidates.append(name)
    for candidate in candidates:
        try:
            if importlib.util.find_spec(candidate) is None:
                continue
        except (ImportError, ValueError):
            continue
        return importlib.import_module(candidate)
    return None


@dataclass
class SeatInfo:
    kind: SeatKind
    name: str
    token: str
    joined: bool = False
    # M2：bot/玩家在该座位的打法方向（建议，不是闸门）
    plan: dict[str, Any] | None = None
    # 聊天侧占用者归因（私聊/群聊 actor + session）；不随公开视图下发
    actor: str = ""
    session_id: str = ""
    # 开局来源：SOURCE_WEBUI / SOURCE_CHAT；空=旧数据（读取时兼容推断）。
    # 显式持久化，避免重启 / 配置变更后把聊天局误判成页面局。
    source: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "joined": self.joined,
            "plan": self.plan,
        }

    def to_dict(self, *, with_token: bool = True) -> dict[str, Any]:
        data = self.to_public()
        data["actor"] = self.actor
        data["session_id"] = self.session_id
        data["source"] = self.source
        if with_token:
            data["token"] = self.token
        return data


@dataclass
class LogEntry:
    seat: Seat
    move: str
    t: int
    # 牌类动作的原始牌 ID（如 ["S3", "H3", "C3", "S4"]）；叫分/过牌/棋类为空。
    # 记录里据此渲染真实牌面（花牌/王插画），同时保留 move 的人话文本。
    cards: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"seat": self.seat, "move": self.move, "t": self.t}
        if self.cards:
            out["cards"] = list(self.cards)
        return out


@dataclass
class MatchChat:
    seat: Seat
    text: str
    t: int
    at: int
    # 发言归属："" = 座位本人；"host" = 宿主主 agent 代该座位发言（S4 代下）。
    # 只由房间层在代下落子时设置（apply_match_action 的 say_by 参数），
    # 客户端动作 dict 传不进来；前端据此把这类 say 归到宿主的教练位气泡。
    by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"seat": self.seat, "text": self.text, "t": self.t, "at": self.at, "by": self.by}


@dataclass
class Match:
    id: str
    kind: str
    created_at: int
    updated_at: int
    version: int
    seats: list[SeatInfo]
    state: Any
    resigned: Seat | None = None
    log: list[LogEntry] = field(default_factory=list)
    chat: list[MatchChat] = field(default_factory=list)
    # 模式选项（S2-S4：mode/role/driver/difficulty/delegation；规范形态见
    # ``modes.normalize_mode_options``）。随快照持久化；旧数据缺省为空 dict，
    # 读取时按座位构成回填（``modes.options_of``）。
    mode_options: dict[str, Any] = field(default_factory=dict)
    # 只属于运行时的旁路字段，不参与 engine 状态；持久化时忽略
    plan: dict[str, Any] | None = None

    # ── 序列化（DB 快照列）──────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "seats": [s.to_dict() for s in self.seats],
            "state": self.state,
            "resigned": self.resigned,
            "log": [e.to_dict() for e in self.log],
            "chat": [c.to_dict() for c in self.chat],
            "mode_options": dict(self.mode_options or {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Match":
        # Minimal shape validation of the persisted snapshot. Nothing is
        # filtered or dropped: a malformed element would shift seat indices
        # (and therefore token/actor ownership), so any bad element rejects
        # the whole row and the store skips it.
        raw_seats = data.get("seats")
        if not isinstance(raw_seats, list) or not raw_seats:
            raise ValueError("persisted match seats is not a non-empty list")
        if not all(isinstance(s, dict) for s in raw_seats):
            raise ValueError("persisted match seat is not an object")
        seats = [
            SeatInfo(
                kind=s.get("kind", "human"),
                name=str(s.get("name") or ""),
                token=str(s.get("token") or ""),
                joined=bool(s.get("joined")),
                plan=s.get("plan"),
                actor=str(s.get("actor") or ""),
                session_id=str(s.get("session_id") or ""),
                source=str(s.get("source") or ""),
            )
            for s in raw_seats
        ]
        raw_log = data.get("log")
        if raw_log is not None and not isinstance(raw_log, list):
            raise ValueError("persisted match log is not a list")
        if raw_log is not None and not all(isinstance(e, dict) for e in raw_log):
            raise ValueError("persisted match log entry is not an object")
        log = [
            LogEntry(
                int(e.get("seat", 0)),
                str(e.get("move", "")),
                int(e.get("t", 0)),
                list(e["cards"]) if isinstance(e.get("cards"), list) else None,
            )
            for e in raw_log or []
        ]
        raw_chat = data.get("chat")
        if raw_chat is not None and not isinstance(raw_chat, list):
            raise ValueError("persisted match chat is not a list")
        if raw_chat is not None and not all(isinstance(c, dict) for c in raw_chat):
            raise ValueError("persisted match chat entry is not an object")
        chat = [
            MatchChat(
                int(c.get("seat", 0)),
                str(c.get("text", "")),
                int(c.get("t", 0)),
                int(c.get("at", 0)),
                str(c.get("by") or ""),
            )
            for c in raw_chat or []
        ]
        state = data.get("state")
        if not isinstance(state, dict):
            raise ValueError("persisted match state is not an object")
        module = _module_or_none(str(data.get("kind") or ""))
        if module is not None:
            problem = _probe_module(module, seats, state, data.get("id"))
            if problem:
                raise ValueError(problem)
        resigned = _clean_resigned(data.get("resigned"), len(seats))
        if resigned is _RESIGNED_BAD:
            # Do not drop the value to ``None``: that would revive a resigned
            # match as active. Normalise it to a canonical out-of-range marker
            # (an int, so it survives the DB integer column and a reload) and
            # let ``status_of`` derive a safe terminal outcome from it.
            already_marked = data.get("resigned") == RESIGNED_INVALID
            if not already_marked:
                logger.warning(
                    "对局 %s 的 resigned 字段非法（%r），按终局安全处理",
                    data.get("id"),
                    data.get("resigned"),
                )
            resigned = RESIGNED_INVALID
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            created_at=int(data.get("created_at") or 0),
            updated_at=int(data.get("updated_at") or 0),
            version=int(data.get("version") or 1),
            seats=seats,
            state=data.get("state"),
            resigned=resigned,
            log=log,
            chat=chat,
            mode_options=(
                dict(data["mode_options"])
                if isinstance(data.get("mode_options"), dict)
                else {}
            ),
        )


@dataclass
class NewSeat:
    kind: SeatKind
    token: str
    name: str | None = None
    joined: bool = False
    actor: str = ""
    session_id: str = ""
    # 开局来源（SOURCE_WEBUI / SOURCE_CHAT）；空=未标注（测试/旧调用）。
    source: str = ""


@dataclass
class NewMatchOptions:
    id: str
    kind: str
    seats: list[NewSeat]
    seed: int
    now: int
    options: dict[str, Any] = field(default_factory=dict)
    # 模式选项（S2-S4；空 dict=legacy 行为，读取时按座位构成回填）
    mode_options: dict[str, Any] = field(default_factory=dict)
    # 宿主 bot 昵称（核心配置）；AI 座位默认显示名取自这里，不硬编码人设名。
    bot_name: str = ""
    # 陪练座位显示名（插件配置，默认「小白」）；多个陪练座位自动加序号。
    practice_name: str = ""
    # ``False`` 时开局不连锁替 bot 座位走子（房间层按节奏自行调度）；
    # 默认 ``True`` 保持引擎既有语义（纯函数/测试直接调用时的行为）。
    autoplay_bots: bool = True


@dataclass
class MatchActionOk:
    match: Match

    ok = True


@dataclass
class MatchActionErr:
    error: str
    message: str

    ok = False


MatchResult = MatchActionOk | MatchActionErr


@dataclass
class MatchStatus:
    waiting_on: list[Seat]
    outcome: Outcome | None
    result_text: str | None


def module_of(kind: str) -> GameModule:
    return games.get(kind)


def clean_name(value: Any) -> str | None:
    """裁剪显示名；空或超长返回 None。"""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text if text and len(text) <= MAX_NAME_LENGTH else None


def clean_options(kind: str, raw: dict[str, Any] | None = None) -> dict[str, str]:
    """只保留已知选项的合法取值，缺失填默认。"""
    raw = raw or {}
    mod = games.get(kind)
    out: dict[str, str] = {}
    for spec in mod.options:
        value = raw.get(spec.key)
        if value is None:
            out[spec.key] = spec.default
            continue
        text = str(value)
        out[spec.key] = (
            text if any(c["value"] == text for c in spec.choices) else spec.default
        )
    return out


def seat_kinds_for(kind: str) -> list[SeatKind]:
    return ["human", "ai", "bot"] if _has_bot(games.get(kind)) else ["human", "ai"]


def default_seat_kinds(kind: str) -> list[SeatKind]:
    mod = games.get(kind)
    out: list[SeatKind] = ["human", "ai"]
    while len(out) < mod.players.default:
        out.append("bot" if _has_bot(mod) else "human")
    return out[: max(mod.players.min, len(out))]


def _has_bot(mod: GameModule) -> bool:
    """模块是否实现了 bot（基类默认返回 None）。"""
    return type(mod).bot is not GameModule.bot


def default_name(
    kind: SeatKind,
    seats: list[NewSeat],
    index: int,
    bot_name: str = "",
    practice_name: str = "",
) -> str:
    nth = sum(1 for s in seats[: index + 1] if s.kind == kind)
    if kind == "bot":
        # 陪练座位的名字来自插件配置（默认「小白」），不硬编码人设名；
        # 同一局多个陪练时第二位起加序号，保证座位可区分。
        base = (practice_name or "").strip() or DEFAULT_PRACTICE_NAME
        return base if nth <= 1 else f"{base} {nth}"
    if kind == "ai":
        # 对局里的 AI 座位就是宿主 bot 本人（玩家口径，页面/日志不出现
        # "AI"）。名字来自核心配置（``bot_name``），缺席才用中性「对手」；
        # 绝不硬编码人设名。将来若出现多 AI 座位，应由该游戏显式给每个
        # 座位发不同的 persona 名，而不是在默认名里编号。
        return (bot_name or "").strip() or "对手"
    return f"朋友 {nth - 1}" if nth > 1 else "Human"


def create_match(opts: NewMatchOptions) -> Match:
    mod = games.get(opts.kind)
    if not mod.ready:
        raise ValueError(f'游戏 "{opts.kind}" 暂不可用。')
    n = len(opts.seats)
    if n < mod.players.min or n > mod.players.max:
        raise ValueError(
            f"{mod.name['zh']}支持 {mod.players.min}-{mod.players.max} 人，收到 {n} 人。"
        )
    if any(s.kind == "bot" for s in opts.seats) and not _has_bot(mod):
        raise ValueError(f"{mod.name['zh']}没有 bot 座位。")
    seats = [
        SeatInfo(
            kind=s.kind,
            name=clean_name(s.name)
            or default_name(
                s.kind, opts.seats, i, opts.bot_name, opts.practice_name
            ),
            token="" if s.kind in ("ai", "bot") else s.token,
            joined=s.kind == "bot" or bool(s.joined),
            actor=s.actor,
            session_id=s.session_id,
            source=s.source,
        )
        for i, s in enumerate(opts.seats)
    ]
    state = mod.create(
        games.create_context(
            seed=opts.seed & 0xFFFFFFFF,
            players=n,
            options=clean_options(opts.kind, opts.options),
        )
    )
    match = Match(
        id=opts.id,
        kind=opts.kind,
        created_at=opts.now,
        updated_at=opts.now,
        version=1,
        seats=seats,
        state=state,
        mode_options=dict(opts.mode_options or {}),
    )
    return autoplay(match, opts.now) if opts.autoplay_bots else match


def status_of(match: Match) -> MatchStatus:
    mod = _module_or_none(match.kind)
    if mod is None:
        # Unknown/unregistered kind (removed game, legacy row): stay terminal
        # instead of raising ``KeyError`` on every read.
        _warn_status_once(
            f"kind:{match.id}:{match.kind}",
            "对局 %s 的游戏类型未知（%s），按终局处理",
            match.id,
            match.kind,
        )
        return MatchStatus(
            waiting_on=[],
            outcome=Outcome(winners=[], text="对局数据异常"),
            result_text="对局数据异常（未知游戏）",
        )
    names = [s.name for s in match.seats]
    seat, unresolvable = _resignation(match)
    if seat is not None:
        return MatchStatus(
            waiting_on=[],
            outcome=_resign_outcome(match, mod, seat),
            result_text=f"{names[seat]} 认输",
        )
    if unresolvable:
        # A row that says "resigned" but whose seat cannot be resolved must
        # stay terminal: returning an active status here would resurrect the
        # match and every later read would re-derive the same broken row.
        _warn_status_once(
            f"resigned:{match.id}:{match.resigned!r}",
            "对局 %s 的认输座位无法解析（%r），按终局处理",
            match.id,
            match.resigned,
        )
        return MatchStatus(
            waiting_on=[],
            outcome=Outcome(winners=[], text="认输"),
            result_text="对局已认输（座位数据异常）",
        )
    try:
        outcome = mod.outcome(match.state)
    except Exception:  # noqa: BLE001 - bad persisted state must not 500 forever
        _warn_status_once(
            f"outcome:{match.id}",
            "对局 %s 的胜负无法派生，按终局处理",
            match.id,
        )
        return MatchStatus(
            waiting_on=[],
            outcome=Outcome(winners=[], text="对局数据异常"),
            result_text="对局数据异常",
        )
    if outcome is None:
        try:
            waiting = mod.waiting_on(match.state)
        except Exception:  # noqa: BLE001 - same boundary as outcome()
            _warn_status_once(
                f"waiting:{match.id}",
                "对局 %s 的等待座位无法派生，按终局处理",
                match.id,
            )
            return MatchStatus(
                waiting_on=[],
                outcome=Outcome(winners=[], text="对局数据异常"),
                result_text="对局数据异常",
            )
        return MatchStatus(waiting_on=waiting, outcome=None, result_text=None)
    who = (
        f"{'、'.join(names[s] for s in outcome.winners)} 胜"
        if outcome.winners
        else "和局"
    )
    return MatchStatus(
        waiting_on=[], outcome=outcome, result_text=f"{who} · {outcome.text}"
    )


def _log_cards(
    kind: str,
    before: Any,
    after: Any,
    seat: Seat | None = None,
    move: str | None = None,
) -> list[str] | None:
    """牌类动作的原始牌 ID（仅本次确实新增了一手出牌时返回）。

    叫分/过牌/认输不改变 ``plays`` 的牌面；只有新增的出牌动作才带 cards，
    避免把上一手的牌错误地挂到叫分记录上。

    斗地主一手打完会立刻换牌（``deal`` 把 ``plays`` 重置为空并 bump
    ``handNo``），此时本手牌面已不在 ``after`` 里，只能从动作文本结合
    出牌前的手牌还原（叫分重发不改 ``handNo``，不会走到这里）。
    """
    if kind != "doudizhu":
        return None
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    after_plays = after.get("plays")
    if not isinstance(after_plays, list):
        return None
    if before.get("handNo") != after.get("handNo"):
        if seat is None or move is None:
            return None
        hands = before.get("cards")
        if not isinstance(hands, list) or not 0 <= seat < len(hands):
            return None
        try:
            return list(parse_cards(hands[seat], move))
        except (TypeError, ValueError):
            return None
    if not after_plays:
        return None
    before_plays = before.get("plays")
    if isinstance(before_plays, list) and len(after_plays) <= len(before_plays):
        return None
    latest = after_plays[-1]
    cards = latest.get("cards") if isinstance(latest, dict) else None
    return list(cards) if isinstance(cards, list) else None


def bot_move(match: Match, seat: Seat) -> str | None:
    """规则 bot 的一手（含 level 难度下可复现的微变数）。

    引擎 ``autoplay`` 与房间层的节奏化步进共用同一取招口径，避免「按节奏
    补手」时丢掉难度扰动。
    """
    mod = games.get(match.kind)
    if not _has_bot(mod):
        return None
    move = mod.bot(match.state, seat)
    if not move:
        return None
    if match.kind in ("gomoku", "reversi") and "level" in match.mode_options:
        assistance = _host_module("assistance")
        ranked_moves = getattr(assistance, "ranked_moves", None)
        import hashlib

        ranked = ranked_moves(match.kind, match.state, seat) if callable(ranked_moves) else []
        # 局面决定的可复现微变数：20%取次优，兜底仍保留原最优bot。
        entropy = int.from_bytes(
            hashlib.sha256(f"{match.id}:{len(match.log)}:{seat}".encode()).digest()[:4],
            "big",
        )
        if len(ranked) > 1 and entropy % 5 == 0:
            move = ranked[1 + entropy % min(2, len(ranked) - 1)]["move"]
    return move


def autoplay(match: Match, now: int) -> Match:
    """让 bot 座位连续走子，直到轮到人/AI 或终局；不 bump version。"""
    mod = games.get(match.kind)
    if not _has_bot(mod):
        return match
    cur = match
    for _ in range(1000):
        status = status_of(cur)
        if status.outcome is not None:
            break
        seat = next(
            (s for s in status.waiting_on if cur.seats[s].kind == "bot"), None
        )
        if seat is None:
            break
        move = bot_move(cur, seat)
        if not move:
            break
        result = mod.apply(cur.state, seat, move)
        if isinstance(result, MoveResultErr):
            break
        entry = LogEntry(
            seat=seat,
            move=result.log or move,
            t=now,
            cards=_log_cards(cur.kind, cur.state, result.state, seat, move),
        )
        entries = [*cur.log, entry]
        # 牌类 6 局可超过 500 个动作；授权以 len(log) 防迟到，不能饱和。
        cur = replace(cur, state=result.state, log=entries if cur.kind == "doudizhu" else entries[-MAX_LOG:])
    return cur


def _append_chat(match: Match, seat: Seat, text: Any, now: int, at: int, by: str = "") -> Match:
    """把一条聊天追加到快照（空文本忽略；超长截断；不 bump version）。"""
    clean = str(text or "").strip()[:MAX_CHAT_LENGTH]
    if not clean:
        return match
    chat = [
        *match.chat,
        MatchChat(seat=seat, text=clean, t=now, at=at, by=str(by or "")[:32]),
    ][-MAX_CHAT:]
    return replace(match, chat=chat)


def apply_match_action(
    match: Match,
    seat: Seat,
    action: dict[str, Any],
    now: int,
    *,
    say_by: str = "",
    autoplay_bots: bool = True,
) -> MatchResult:
    """统一动作：move / resign / say / rename。

    ``move`` 动作可带可选 ``say``：落子成功后，在同一次快照变更里追加一条
    对局聊天（``MatchChat``），**不会产生第二次动作/版本推进**。落子失败
    时 ``say`` 绝不落库，避免「棋没下出去、话先说了」的竞态。

    ``say_by`` 标记这条 say 的真实说话人（"" = 座位本人；"host" = 代下），
    只对房间层开放的关键字参数：客户端动作 dict 里就算带上同名字段也不
    生效，防止伪造「宿主代下」发言归属。
    """
    if not isinstance(seat, int) or seat < 0 or seat >= len(match.seats):
        return MatchActionErr("bad_seat", "没有这个座位")

    def bump(patch: dict[str, Any]) -> Match:
        # ``dataclasses.replace`` keeps every existing field in sync: a new
        # field can never be silently dropped from a hand-written list.
        return replace(match, updated_at=now, version=match.version + 1, **patch)

    kind = str(action.get("type") or "")
    status = status_of(match)

    if kind == "move":
        if status.outcome is not None:
            return MatchActionErr("game_over", "对局已经结束")
        move = str(action.get("move") or "").strip()
        if not move:
            return MatchActionErr("empty_move", "没有写这步棋")
        result = games.get(match.kind).apply(match.state, seat, move)
        if isinstance(result, MoveResultErr):
            return MatchActionErr("illegal", result.error)
        log = [
            *match.log,
            LogEntry(
                seat=seat,
                move=result.log or move,
                t=now,
                cards=_log_cards(match.kind, match.state, result.state, seat, move),
            ),
        ]
        if match.kind != "doudizhu":
            log = log[-MAX_LOG:]
        moved = bump({"state": result.state, "log": log})
        # 原子 say：只有合法落子才追加；与落子共用同一个 version/updated_at。
        moved = _append_chat(moved, seat, action.get("say"), now, len(log), by=say_by)
        # ``autoplay_bots=False`` 让房间层按节奏（1-2 秒）自行调度 bot 应手，
        # 而不是在同一个请求里把 bot 的下一手瞬间补上。
        return MatchActionOk(autoplay(moved, now) if autoplay_bots else moved)

    if kind == "resign":
        if status.outcome is not None:
            return MatchActionErr("game_over", "对局已经结束")
        log = [*match.log, LogEntry(seat=seat, move="resign", t=now)]
        # Doudizhu keeps every move (authorisation relies on len(log)); the
        # other games cap the log, same as their move path.
        if match.kind != "doudizhu":
            log = log[-MAX_LOG:]
        return MatchActionOk(bump({"resigned": seat, "log": log}))

    if kind == "say":
        text = str(action.get("text") or "").strip()[:MAX_CHAT_LENGTH]
        if not text:
            return MatchActionErr("empty", "消息是空的")
        chat = [
            *match.chat,
            MatchChat(seat=seat, text=text, t=now, at=len(match.log)),
        ][-MAX_CHAT:]
        return MatchActionOk(bump({"chat": chat}))

    if kind == "rename":
        target = action.get("seat")
        try:
            target = seat if target is None else int(target)
        except (TypeError, ValueError):
            return MatchActionErr("bad_seat", "没有这个座位")
        if not (0 <= target < len(match.seats)):
            return MatchActionErr("bad_seat", "没有这个座位")
        name = clean_name(action.get("name"))
        if not name:
            return MatchActionErr("bad_name", "名字要 1 到 40 个字")
        seats = [
            replace(s, name=name) if i == target else s
            for i, s in enumerate(match.seats)
        ]
        return MatchActionOk(bump({"seats": seats}))

    return MatchActionErr("bad_action", "看不懂这个动作")


def seat_by_token(match: Match, token: str | None) -> Seat | None:
    if not token:
        return None
    for i, s in enumerate(match.seats):
        if s.token and s.token == token:
            return i
    return None


def join_seat(match: Match, seat: Seat, now: int) -> Match:
    """标记座位已被占用。"""
    if match.seats[seat].joined:
        return match
    seats = [
        replace(s, joined=True) if i == seat else s for i, s in enumerate(match.seats)
    ]
    return replace(
        match, seats=seats, updated_at=now, version=match.version + 1
    )


def open_seat(match: Match, kind: SeatKind) -> Seat | None:
    for i, s in enumerate(match.seats):
        if s.kind == kind and not s.joined:
            return i
    return None


def view_match(match: Match, viewer: Seat | None) -> dict[str, Any]:
    """一个座位（或观战 None）可见的视图：剥 state/token，插入 me/view/status/labels。"""
    mod = _module_or_none(match.kind)
    status = status_of(match)
    # UI / SSE 规范模式选项（与 GET /mode 同源）；旧局按座位/kind 读时回填。
    modes_module = _host_module("modes")
    options_of = getattr(modes_module, "options_of", None)
    mode_options = (
        options_of(match)
        if callable(options_of)
        else dict(getattr(match, "mode_options", None) or {})
    )

    # Dou-dizhu's lobby summary exposes the configured hand count. Persisted
    # state is validated minimally at deserialization, but this is still a
    # read path: never index a missing key (a corrupt row must not 500 the view).
    hands_options: dict[str, Any] = {}
    if match.kind == "doudizhu" and isinstance(match.state, dict):
        hands = match.state.get("hands")
        if hands is not None:
            hands_options = {"options": {"hands": str(hands)}}
    return {
        "id": match.id,
        "kind": match.kind,
        "created_at": match.created_at,
        "updated_at": match.updated_at,
        "version": match.version,
        "mode_options": mode_options,
        **hands_options,
        "seats": [s.to_public() for s in match.seats],
        "resigned": _resignation(match)[0],
        "log": [e.to_dict() for e in match.log],
        "chat": [c.to_dict() for c in match.chat],
        "me": viewer,
        # Unknown kind: no module view exists, but the read path must not 500.
        "view": mod.view(match.state, viewer) if mod is not None else {},
        "status": {
            "waitingOn": status.waiting_on,
            "outcome": (
                None
                if status.outcome is None
                else {
                    "winners": status.outcome.winners,
                    "text": status.outcome.text,
                }
            ),
            "resultText": status.result_text,
        },
        "labels": mod.seat_labels(match.state) if mod is not None else [],
    }


_KIND_EN: dict[str, str] = {"human": "human", "ai": "AI", "bot": "bot"}


def describe_match(
    match: Match, seat: Seat, page_url: str | None = None
) -> str:
    """给 AI 的完整局面文本（英文，rain-go 原文风格）。"""
    mod = games.get(match.kind)
    labels = mod.seat_labels(match.state)
    status = status_of(match)
    names = [s.name for s in match.seats]
    out = [
        f"Game {match.id} · {mod.name['en']} ({mod.name['zh']}) · {len(match.seats)} players"
    ]
    if page_url:
        out.append(f"Page: {page_url}")
    out.append(f"You are {names[seat]} in seat {seat} ({labels[seat] if seat < len(labels) else ''}).")
    out.append(
        "Seats: "
        + " · ".join(
            f"{i} {s.name} ({_KIND_EN.get(s.kind, s.kind)}"
            f"{', ' + labels[i] if i < len(labels) and labels[i] else ''}"
            f"{'' if s.joined else ', not joined yet'})"
            for i, s in enumerate(match.seats)
        )
    )
    rules = str(getattr(mod, "rules", "") or "").strip()
    if rules:
        out.append("")
        out.append("Rules:")
        out.append(rules)
    if status.result_text:
        out.append(f"GAME OVER: {status.result_text}.")
    elif seat in status.waiting_on:
        out.append(
            f"Status: YOUR TURN. Move syntax: {mod.move_help}"
        )
    else:
        out.append(
            f"Status: waiting for {', '.join(names[s] for s in status.waiting_on)}."
        )
    out.append("")
    out.append(mod.describe(match.state, seat, names))
    recent = match.chat[-6:]
    if recent:
        out.append("")
        out.append("Recent messages:")
        for c in recent:
            who = f"{names[c.seat]} (you)" if c.seat == seat else names[c.seat]
            out.append(f"- {who}: {c.text}")
    return "\n".join(out)


def agent_view_text(
    match: Match,
    seat: Seat,
    *,
    plan_text: str = "",
    plan_review: bool = False,
    chat_limit: int = 3,
    threat_analysis: bool = True,
    include_rules: bool = True,
    include_move_syntax: bool = True,
    include_instructions: bool = True,
) -> str:
    """``game_agent`` 的整盘 user prompt（不含旧 ``MOVE:`` 文本协议）。

    输出结构固定为：规则 → 当前局面/最近聊天 → 计划/游戏策略 → 威胁分析
    → Bot 前三候选（``assistance.recommendation_text``，仅高级档/代打座位）。
    规则来自 ``mod.rules``，坐标说明来自 ``mod.move_help``；棋盘只在
    「当前局面」块由 ``mod.describe`` 输出一次，身份/座位/状态行保持原有
    口径，不会出现两套规则文本漂移。计划块内先注入有效 ``plan_text``，
    再注入游戏模块的 ``plan_prompt`` 动态策略；没有 plan 时不伪造
    ``Current plan``，只显示动态策略（若模块提供）。

    ``threat_analysis=False`` 时完全不调用游戏模块的威胁摘要（也不调用
    assistance 等级判定）；模块没有 ``threat_summary`` 时同样直接跳过，
    因此未来没有该能力的游戏不会报错。``threat_analysis=True`` 时棋类
    仍受该局等级约束（beginner 关闭、代打人类座位强制开启）。

    Default output stays complete (rules / move format / seat identity and
    turn instructions), so existing callers keep their behaviour.
    ``game_agent`` moves the stable items into the per-match prefix and passes
    the matching flags as ``False`` explicitly, leaving the dynamic snapshot
    with the current position, public chat, plan and threat only. This avoids
    repeating rule/format/identity/speech instructions within one request.
    ``include_instructions=False`` also drops the seat identity line, the
    ``YOUR TURN`` play/speech instruction and the doudizhu table-talk block;
    only ``Status: YOUR TURN.`` remains as the action-right fact. Seat labels
    (landlord/farmer) are dynamic sides and remain in the current snapshot's
    Seats/position block.
    """
    mod = games.get(match.kind)
    labels = mod.seat_labels(match.state)
    status = status_of(match)
    names = [s.name for s in match.seats]
    out = [
        f"Game {match.id} · {mod.name['en']} ({mod.name['zh']}) · "
        f"{len(match.seats)} players"
    ]
    if include_instructions:
        out.append(
            f"You are {names[seat]} in seat {seat}"
            f" ({labels[seat] if seat < len(labels) else ''})."
        )
    out.append(
        "Seats: "
        + " · ".join(
            f"{i} {s.name} ({_KIND_EN.get(s.kind, s.kind)}"
            f"{', ' + labels[i] if i < len(labels) and labels[i] else ''}"
            f"{'' if s.joined else ', not joined yet'})"
            for i, s in enumerate(match.seats)
        )
    )
    rules = str(getattr(mod, "rules", "") or "").strip()
    if rules and include_rules:
        out.append("")
        out.append("Rules:")
        out.append(rules)
    move_help = str(getattr(mod, "move_help", "") or "").strip()
    if move_help and include_move_syntax:
        out.append(f"Move syntax: {move_help}.")
    out.append("")
    out.append("Current position:")
    if status.result_text:
        out.append(f"GAME OVER: {status.result_text}.")
    elif seat in status.waiting_on:
        if include_instructions:
            out.append(
                "Status: YOUR TURN. Call the `play` tool with your move now, "
                "and include one or two short player-style lines in the same "
                "reply; do not reply with text only."
            )
        else:
            # Action right only: speech/play instructions live in the stable
            # prefix, never in the per-turn snapshot.
            out.append("Status: YOUR TURN.")
    else:
        out.append(
            f"Status: waiting for {', '.join(names[s] for s in status.waiting_on)}."
        )
    description = mod.describe(match.state, seat, names)
    if not include_instructions:
        # The snapshot must carry the position only. Some modules embed a
        # speech constraint in their private-hand label; the stable prefix
        # already owns that boundary, so drop the parenthetical here.
        description = description.replace(
            " (do not repeat in table speech)", ""
        )
    out.append(description)
    if match.kind == "doudizhu" and include_instructions:
        # 牌类有隐藏信息：规则里“只能聊公开动作/张数”容易被读成“不要
        # 说话”。这里给一条正向许可，配合系统 prompt 的同轮短话要求，
        # 让模型知道可以聊公开信息与气氛，只是不能报私牌。
        out.append("")
        out.append(
            "Table talk: you may comment on public plays, card counts and "
            "the mood in one or two short lines; never reveal your private "
            "hand or strategy."
        )
    recent = match.chat[-chat_limit:] if chat_limit > 0 else []
    if recent:
        out.append("")
        out.append("Recent messages:")
        for c in recent:
            who = f"{names[c.seat]} (you)" if c.seat == seat else names[c.seat]
            out.append(f"- {who}: {c.text}")
    plan_lines: list[str] = []
    if plan_text:
        review = "（局面可能已变化，必要时可调整，但仍优先执行计划）" if plan_review else ""
        plan_lines.append(
            f"Current plan: {plan_text}{review}。"
            "这是本局策略指令：即使偏离默认目标，也优先执行；"
            "它不会改变游戏规则与动作合法性。"
        )
    plan_fn = getattr(mod, "plan_prompt", None)
    if callable(plan_fn):
        try:
            strategy = str(plan_fn(match.state, seat) or "").strip()
        except Exception:
            strategy = ""
        if strategy:
            plan_lines.append(f"Game strategy: {strategy}")
    if plan_lines:
        out.append("")
        out.extend(plan_lines)
    assistance = _host_module("assistance")
    analysis_enabled = getattr(assistance, "analysis_enabled", None)
    recommendation_text = getattr(assistance, "recommendation_text", None)
    if match.kind in ("gomoku", "reversi") and callable(analysis_enabled):
        # ``threat_analysis=False`` is a hard opt-out: the level only *adds*
        # analysis, it can never override an explicit request to skip it.
        threat_analysis = threat_analysis and bool(analysis_enabled(match, seat))
    if threat_analysis:
        summary_fn = getattr(mod, "threat_summary", None)
        threat_text = ""
        if callable(summary_fn):
            try:
                threat_text = str(summary_fn(match.state, seat) or "").strip()
            except Exception:
                threat_text = ""
        if threat_text:
            out.append("")
            out.append(
                "Threat analysis (based on the current game state only; "
                "informational, it cannot override the plan):"
            )
            out.append(threat_text)
    recommendation = (
        recommendation_text(match, seat) if callable(recommendation_text) else ""
    )
    if recommendation:
        out.extend(["", recommendation])
    return "\n".join(out)
