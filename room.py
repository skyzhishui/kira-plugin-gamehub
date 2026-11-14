"""对局房间层（设计 §5.3）：内存 Map + 每局串行锁 + WS 扇出 + 快照落库。

纪律（实现与设计一致）：

- **单 actor 单局**：``RoomManager.create`` 遇同 actor 的 active 局抛
  ``actor_busy``（调用方改为返回该局）；全局 active 数达
  ``config.max_active_matches`` 抛 ``match_full``，不做 LRU 淘汰。
- **人端版本**：REST 提交带客户端所见 ``client_version``，房间锁内比对，
  不匹配抛 ``stale_version``；AI/内部路径（``try_apply_ai``）不带版本。
- **落库 last-write-wins**：锁内 ``await repository.save(match, status)``；
  成功才广播；失败记 warning、``dirty=True``（内存为准），下次变更整行
  补写；``flush`` 无视 dirty 强制写。
- **广播**：每个 socket 一个发送队列 + 发送任务，per-socket 保序；发送
  失败移除 socket；队列投递不阻塞房间锁。
- **ai_pending**：锁内 check-and-add；``try_apply_ai`` 成功/终局时移除；
  可选 ``drive_ai_hook`` 供 M2 的 ai_player 注入（M1 默认 None，仅保留
  结构与不变量）。

所有对外视图都经 ``engine.match.view_match`` / ``SeatInfo.to_public``，
口令（token）不进入任何广播或 ``public_state``。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

try:  # production: plugin package (``plugins.<plugin>.room``)
    from . import modes
    from .engine import games
    from .engine.match import (
        Match,
        NewMatchOptions,
        NewSeat,
        apply_match_action,
        create_match,
        seat_by_token,
        status_of,
        view_match,
    )
    from .engine.types import GameModule
    from .modes import normalize_mode_options
    from .plan import make_plan, normalize_plan_text, plan_remaining
except ImportError:  # standalone tests import this module as top-level
    import modes
    from engine import games
    from engine.match import (
        Match,
        NewMatchOptions,
        NewSeat,
        apply_match_action,
        create_match,
        seat_by_token,
        status_of,
        view_match,
    )
    from engine.types import GameModule
    from modes import normalize_mode_options
    from plan import make_plan, normalize_plan_text, plan_remaining

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

CLOSE_STOPPING = 4003
_SOCKET_QUEUE_MAX = 64
_STOP = object()

# 已结束房间的内存保留（不做新调度器）：无客户端连接的终局房间在宽限后
# 从内存剔除；有连接时保留，迟到的 GET/WS 仍按 id 从仓储惰性加载。
_OVER_ROOM_GRACE_S = 300.0
_MAX_OVER_ROOMS = 64

# 进阶陪练座位默认显示名；真实身份由上层（main/game_agent 配置）通过
# ``RoomManager(opponent_name=...)`` / ``create(opponent_name=...)`` 注入。
DEFAULT_OPPONENT_NAME = "陪练"

# 规则陪练座位默认显示名（插件配置 ``practice_name`` 可覆盖）。
DEFAULT_PRACTICE_NAME = "小白"


def _default_now() -> int:
    """毫秒时间戳（与 engine 的 ``t`` / ``created_at`` 单位一致）。"""
    return int(time.time() * 1000)


def _has_bot(module: GameModule) -> bool:
    return type(module).bot is not GameModule.bot


def _probe_seats(opponent: str, total: int) -> list[Any]:
    """开局前的座位构成探测（SimpleNamespace 形状，供模式归一校验）。"""
    from types import SimpleNamespace

    kinds = ["human"] + [
        (opponent if index == 1 else "ai") for index in range(1, max(total, 2))
    ]
    return [SimpleNamespace(kind=k) for k in kinds]


async def _maybe_await(value: Any) -> Any:
    """兼容同步/异步的仓储钩子（测试假仓储可以是异步、真实仓储也异步）。"""
    if inspect.isawaitable(value):
        return await value
    return value


class RoomError(Exception):
    """对局层业务错误：``code`` 供 REST/工具映射，``message`` 可直接给用户。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _SocketWriter:
    queue: asyncio.Queue[Any] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_SOCKET_QUEUE_MAX)
    )
    task: asyncio.Task[Any] | None = None
    closed: bool = False


class GameRoom:
    """一局的内存宿主：锁、socket 扇出、落库、AI 待办集合。"""

    def __init__(
        self,
        match: Match,
        *,
        repository: Any,
        now_fn: Callable[[], int] | None = None,
        drive_ai_hook: Callable[["GameRoom", int], Any] | None = None,
    ) -> None:
        self.match = match
        self.repository = repository
        self.now_fn = now_fn or _default_now
        # M2 注入：``drive_ai_hook(room, seat)``；M1 默认 None（不自动驱动 AI）。
        self.drive_ai_hook = drive_ai_hook
        # M1.5 注入：``event_hook(room, event)``（table_talk 消费）；None=不发通知。
        self.event_hook: Callable[["GameRoom", dict[str, Any]], Any] | None = None

        self.sockets: dict[Any, int | None] = {}
        self.lock = asyncio.Lock()
        self.ai_pending: set[int] = set()
        self.dirty = True
        self.status = "over" if status_of(match).outcome is not None else "active"
        # 设计 §5.3 的软在途锁字段（M3 table_talk 使用；此处仅保留状态位）。
        self.last_talk_ts = 0.0
        self.last_plan_ts = 0.0

        self._writers: dict[Any, _SocketWriter] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        # AI 决策驱动任务（认输时取消/安全丢弃；不含 WS/事件任务）。
        self._drive_tasks: set[asyncio.Task[Any]] = set()
        # 每座位驱动代次：自动 pass 连走会立即再排一次驱动，旧任务收尾时
        # 不得把新任务登记的 ai_pending 清掉（否则可能并发重复决策）。
        self._drive_gen: dict[int, int] = {}

    # ------------------------------------------------------------------
    #  视图
    # ------------------------------------------------------------------
    def view(self, viewer: int | None) -> dict[str, Any]:
        """某座位（None=观战）可见的完整 MatchView。"""
        return view_match(self.match, viewer)

    def public_state(self) -> dict[str, Any]:
        """大厅/工具摘要：不含 token（``SeatInfo.to_public`` 已剥口令）。"""
        status = status_of(self.match)
        return {
            "id": self.match.id,
            "kind": self.match.kind,
            "version": self.match.version,
            "status": self.status,
            "over": status.outcome is not None,
            "created_at": self.match.created_at,
            "updated_at": self.match.updated_at,
            "seats": [s.to_public() for s in self.match.seats],
            "labels": games.get(self.match.kind).seat_labels(self.match.state),
            "waitingOn": list(status.waiting_on),
            "outcome": (
                None
                if status.outcome is None
                else {
                    "winners": list(status.outcome.winners),
                    "text": status.outcome.text,
                }
            ),
            "resultText": status.result_text,
            "moves": len(self.match.log),
        }

    def status_info(self) -> Any:
        """当前对局状态（table_talk 等只读消费者用）。"""
        return status_of(self.match)

    # ------------------------------------------------------------------
    #  事件钩子（table_talk）
    # ------------------------------------------------------------------
    def notify_event(self, event: dict[str, Any]) -> None:
        """异步派发对局事件；不阻塞调用方（turn 主路径/房间锁）。"""
        hook = self.event_hook
        if hook is None:
            return
        task = asyncio.create_task(self._run_event(event), name=f"gamehub-event-{self.match.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_event(self, event: dict[str, Any]) -> None:
        hook = self.event_hook
        if hook is None:
            return
        try:
            await _maybe_await(hook(self, event))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 事件消费失败不影响对局
            logger.warning("对局事件派发失败（%s）", self.match.id, exc_info=True)

    # ------------------------------------------------------------------
    #  WS 接入/断开
    # ------------------------------------------------------------------
    async def attach(self, ws: Any, seat: int | None) -> None:
        """绑定 socket 到座位（None=观战）并下发首帧全量视图。"""
        old_writer: _SocketWriter | None = None
        async with self.lock:
            if seat is not None and (
                not isinstance(seat, int) or seat < 0 or seat >= len(self.match.seats)
            ):
                seat = None
            old_writer = self._writers.pop(ws, None)
            if old_writer is not None:
                old_writer.closed = True

            self.sockets[ws] = seat
            writer = _SocketWriter()
            self._writers[ws] = writer
            task = asyncio.create_task(
                self._send_loop(ws, writer), name=f"gamehub-ws-{self.match.id}"
            )
            writer.task = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            self._enqueue_locked(ws, {"type": "match", "match": self.view(seat)})
        if old_writer is not None:
            await self._stop_writer(old_writer)

    async def detach(self, ws: Any) -> None:
        await self._drop_socket(ws)

    async def _drop_socket(self, ws: Any) -> None:
        async with self.lock:
            self.sockets.pop(ws, None)
            writer = self._writers.pop(ws, None)
            if writer is not None:
                writer.closed = True
        if writer is not None:
            await self._stop_writer(writer)

    async def _stop_writer(self, writer: _SocketWriter) -> None:
        task = writer.task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _send_loop(self, ws: Any, writer: _SocketWriter) -> None:
        try:
            while True:
                item = await writer.queue.get()
                if item is _STOP or writer.closed:
                    return
                try:
                    await self._send_one(ws, item)
                except Exception:  # noqa: BLE001 - 连接已断/序列化失败
                    logger.debug("WS 发送失败，移除 socket: %s", self.match.id, exc_info=True)
                    await self._drop_socket(ws)
                    return
        finally:
            writer.closed = True

    @staticmethod
    async def _send_one(ws: Any, payload: dict[str, Any]) -> None:
        send_json = getattr(ws, "send_json", None)
        if callable(send_json):
            await send_json(payload)
            return
        send_str = getattr(ws, "send_str", None)
        if callable(send_str):
            await send_str(json.dumps(payload, ensure_ascii=False))
            return
        raise TypeError("socket 缺少 send_json/send_str")

    def _enqueue_locked(self, ws: Any, payload: dict[str, Any]) -> None:
        writer = self._writers.get(ws)
        if writer is None or writer.closed:
            return
        queue = writer.queue
        if queue.full():
            # 慢客户端：丢最旧的一帧，保留最新视图（队列仍保序）。
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(payload)

    def _broadcast_locked(self) -> None:
        for ws, seat in list(self.sockets.items()):
            self._enqueue_locked(ws, {"type": "match", "match": self.view(seat)})

    async def close_all(self, code: int = CLOSE_STOPPING, reason: str = "plugin stopping") -> None:
        """关闭全部 WS（插件停用）：先停发送任务，再 close(code)。"""
        async with self.lock:
            socks = list(self.sockets)
            writers = list(self._writers.values())
            self.sockets.clear()
            self._writers.clear()
            for writer in writers:
                writer.closed = True

        pending = [w.task for w in writers if w.task is not None and not w.task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for ws in socks:
            try:
                await ws.close(code=code, message=reason.encode("utf-8"))
            except Exception:  # noqa: BLE001 - 已断开的 socket
                logger.debug("close WS 失败: %s", self.match.id, exc_info=True)

    # ------------------------------------------------------------------
    #  动作
    # ------------------------------------------------------------------
    async def apply(
        self,
        seat: int,
        action: dict[str, Any],
        *,
        client_version: int | None = None,
        now: int | None = None,
    ) -> Match:
        """锁内校验版本 → 应用动作 → 落库 → 成功才广播。"""
        drive_seats: list[int] = []
        event: dict[str, Any] | None = None
        async with self.lock:
            if client_version is not None and client_version != self.match.version:
                raise RoomError("stale_version", "页面已过期，请刷新后重试")
            when = self.now_fn() if now is None else int(now)
            before_state = self.match.state
            result = apply_match_action(self.match, seat, action, when)
            if not result.ok:
                raise RoomError(result.error, result.message)
            self.match = result.match
            saved = await self._persist_locked(force=True)
            self._refresh_status_locked()
            if saved:
                self._broadcast_locked()
            drive_seats = self._check_ai_locked()
            action_type = str(action.get("type") or "")
            if action_type in ("move", "resign"):
                last = self.match.log[-1].move if self.match.log else ""
                event = {
                    "type": action_type,
                    "seat": seat,
                    "move": last,
                    "over": status_of(self.match).outcome is not None,
                    # 绑定当时快照：异步事件晚执行时不得读已变化的棋盘。
                    "match_id": self.match.id,
                    "version": self.match.version,
                    "match": self.match,
                    "before_state": before_state,
                }
            match = self.match

        self._spawn_drive(drive_seats)
        if event is not None:
            self.notify_event(event)
        return match

    async def set_plan(
        self,
        seat: int,
        *,
        text: str,
        ttl_moves: int,
        ttl_s: float,
        source: str = "agent",
        review: bool = False,
        now: int | None = None,
    ) -> Match:
        """锁内校验并写入 ``seat.plan``：只持久化，**不 bump version / 不改走子**。

        ``created_move`` / ``seq`` 必须在本房间锁内构造，避免两个并发写拿到同一
        seq；对局已结束或目标不是宿主主 agent 座位时拒绝写入。plan 是给 ``game_agent``
        的战术提示（不是闸门），人类侧不应因它收到 409；因此不广播、不更新
        ``updated_at``，页面下次拉快照即可看到。
        """
        async with self.lock:
            if self.status == "over" or status_of(self.match).outcome is not None:
                raise RoomError("game_over", "对局已经结束，不能再设置打法")
            if not isinstance(seat, int) or seat < 0 or seat >= len(self.match.seats):
                raise RoomError("bad_seat", "没有这个座位")
            target = self.match.seats[seat]
            if target.kind != "ai":
                raise RoomError(
                    "no_ai_seat",
                    f"当前对局里没有{self.bot_name}的座位（对手是规则陪练或真人），不能设置打法",
                )
            when = self.now_fn() if now is None else int(now)
            plan = make_plan(
                text,
                source=source,
                created_move=len(self.match.log),
                ttl_moves=ttl_moves,
                ttl_s=ttl_s,
                now=when,
                previous=target.plan,
                review=review,
            )
            seats = list(self.match.seats)
            seats[seat] = replace(target, plan=plan)
            self.match = replace(self.match, seats=seats)
            await self._persist_locked(force=True)
            return self.match

    async def mark_plan_review(self, seat: int) -> bool:
        """锁内给**当前** ``seat.plan`` 打 ``review=True`` 软标记。

        notice judge 的关键事件判定命中时调用，供下一次 game_agent 决策
        参考（评分阈值配置已在移植中随 System1 一起移除）。关键约束：

        - 只读锁内**最新** plan，不拿 judge 手里的旧快照覆盖新 ``game_plan``；
        - 不改 ``seq``、不 bump ``version``、不广播（与 ``set_plan`` 同口径）；
        - 没有 plan / 已过期 / 对局结束 → 不写入，返回 False。
        """
        async with self.lock:
            if self.status == "over" or status_of(self.match).outcome is not None:
                return False
            if not isinstance(seat, int) or not (0 <= seat < len(self.match.seats)):
                return False
            current = self.match.seats[seat]
            plan = current.plan
            if not isinstance(plan, dict) or not normalize_plan_text(plan.get("text")):
                return False
            try:
                tempo = str(games.get(self.match.kind).tempo or "turn")
            except Exception:  # noqa: BLE001 - tempo 异常按回合制
                tempo = "turn"
            ttl = plan_remaining(
                plan,
                tempo=tempo,
                move_count=len(self.match.log),
                now=self.now_fn(),
            )
            if not ttl.get("active"):
                return False
            if plan.get("review"):
                return True
            seats = list(self.match.seats)
            seats[seat] = replace(current, plan={**plan, "review": True})
            self.match = replace(self.match, seats=seats)
            await self._persist_locked(force=True)
            return True

    async def ai_snapshot(self, seat: int) -> dict[str, Any]:
        """锁内读取 AI 决策所需快照（不持锁做网络/LLM 调用）。

        与 ``apply_ai_move`` 的提交校验同源：快照只用于构造 prompt，
        真正提交时仍会在锁内重读 status/座位/手数，防止迟到决策跨回合。
        """
        async with self.lock:
            return self._seat_snapshot_locked(seat, expect_kind="ai")

    async def turn_snapshot(self, seat: int) -> dict[str, Any]:
        """锁内读取任一座位的回合快照（不校验座位 kind）。

        供行动通知回合（S2 宿主主 agent 座位 / S4 受托人类座位）在锁外构造通知
        与 watchdog 判断使用；字段与 ``ai_snapshot`` 完全同源。
        """
        async with self.lock:
            return self._seat_snapshot_locked(seat, expect_kind=None)

    def _seat_snapshot_locked(
        self, seat: int, *, expect_kind: str | None
    ) -> dict[str, Any]:
        status = status_of(self.match)
        valid = (
            self.status != "over"
            and status.outcome is None
            and isinstance(seat, int)
            and 0 <= seat < len(self.match.seats)
            and (expect_kind is None or self.match.seats[seat].kind == expect_kind)
            and seat in status.waiting_on
        )
        return {
            "match": self.match,
            "active": valid,
            "move_count": len(self.match.log),
            "version": self.match.version,
            "waiting_on": list(status.waiting_on),
        }

    async def apply_ai_move(
        self,
        seat: int,
        move: str,
        *,
        expected_move_count: int | None = None,
        say: str | None = None,
        persist_say: bool = True,
    ) -> tuple[Match | None, str]:
        """AI 内部落子：锁内二次校验，成功返回 ``(match, "")``，失败返回错误文本。

        与 ``try_apply_ai`` 共用同一条锁内落子路径，差别只在失败时把
        ``apply_match_action`` 的可读错误（如「这里已经有子了」）带出，
        供 ``game_agent`` 的下一轮纠正。``expected_move_count`` 用于拒绝
        「LLM 思考期间局面已被推进」的迟到提交；非法落子不清理
        ``ai_pending``，让同一决策任务继续纠正，最终由驱动任务 finally 清理。

        ``say``（可选）是**原子**随落子的对局聊天：只有落子成功才追加到
        ``Match.chat``，与落子共用一次版本推进；失败/迟到绝不落库。
        ``persist_say=False`` 目前没有调用方；行动回合的 say 由
        ``play_<match>`` 原子随落子写入对局聊天，宿主的普通正文回复另行走
        会话面，不经此参数。
        """
        event: dict[str, Any] | None = None
        follow: list[int] = []
        async with self.lock:
            status = status_of(self.match)
            if self.status == "over" or status.outcome is not None:
                self.ai_pending.discard(seat)
                return None, "对局已经结束"
            if not isinstance(seat, int) or not (0 <= seat < len(self.match.seats)):
                self.ai_pending.discard(seat)
                return None, "没有这个座位"
            if self.match.seats[seat].kind != "ai":
                self.ai_pending.discard(seat)
                return None, f"这不是{self.bot_name}的座位"
            if seat not in status.waiting_on:
                self.ai_pending.discard(seat)
                return None, "现在还没轮到你（或这手已经过期）"
            if expected_move_count is not None and len(self.match.log) != int(
                expected_move_count
            ):
                self.ai_pending.discard(seat)
                return None, "局面已经变化（等待期间已有人落子），本手作废"
            before_state = self.match.state
            before_chat = len(self.match.chat)
            action: dict[str, Any] = {"type": "move", "move": move}
            if say and persist_say:
                action["say"] = say
            result = apply_match_action(self.match, seat, action, self.now_fn())
            if not result.ok:
                # 非法落子：保留 ai_pending，允许同一决策任务纠正后重试。
                return None, result.message
            self.match = result.match
            saved = await self._persist_locked(force=True)
            self._refresh_status_locked()
            if saved:
                self._broadcast_locked()
            self.ai_pending.discard(seat)
            # 自动 pass（黑白棋）可能让同一 AI 座位继续行棋：由房间层重新
            # 登记驱动，避免「AI 落子后仍轮到 AI」却没有下一次决策任务。
            follow = self._check_ai_locked()
            applied_say = ""
            if persist_say and len(self.match.chat) > before_chat:
                applied_say = str(self.match.chat[-1].text or "")
            event = {
                "type": "move",
                "seat": seat,
                "move": move,
                "say": applied_say,
                "over": status_of(self.match).outcome is not None,
                "match_id": self.match.id,
                "version": self.match.version,
                "match": self.match,
                "before_state": before_state,
            }
            match = self.match
        self._spawn_drive(follow)
        if event is not None:
            self.notify_event(event)
        return match, ""

    async def try_apply_ai(self, seat: int, move: str) -> Match | None:
        """AI 内部路径：锁内重读 status，已结束/不在等待位 -> None。"""
        match, _error = await self.apply_ai_move(seat, move)
        return match

    async def apply_delegated_move(
        self,
        seat: int,
        move: str,
        *,
        expected_move_count: int | None = None,
        grant_key: str = "",
        say: str | None = None,
        persist_say: bool = True,
    ) -> tuple[Match | None, str]:
        """受托人类座位落子（S4）：锁内校验委托授权，成功返回 ``(match, "")``。

        与 ``apply_ai_move`` 的差别与纪律：

        - **只接受 human 座位**：引擎 ``autoplay`` 绝不自动替人类座位走子
          （``Avoid human seat auto engine stepping``），这里的多一步必须来自
          显式委托授权（``modes.delegation`` 开启时由 TurnDirector 发放的
          ``grant_key``），不是引擎步进；
        - ``expected_move_count`` / 迟到防护语义与 AI 路径一致：人类手动落子
          （页面/WS）先到时，本路径按「局面已经变化」拒绝；授权在位（stop
          只是停发**下一轮**授权）时本手允许完成；
        - ``grant_key`` 校验由调用方（TurnDirector）在锁外完成（授权映射是
          director 状态）；房间层只负责座位 kind / 等待位 / 手数校验。
        - ``say`` / ``persist_say`` 语义与 ``apply_ai_move`` 完全一致：只有
          合法落子才在同一快照里追加对局聊天。
        """
        event: dict[str, Any] | None = None
        async with self.lock:
            status = status_of(self.match)
            if self.status == "over" or status.outcome is not None:
                return None, "对局已经结束"
            if not isinstance(seat, int) or not (0 <= seat < len(self.match.seats)):
                return None, "没有这个座位"
            if self.match.seats[seat].kind != "human":
                return None, "这不是可以代下的人类座位"
            if seat not in status.waiting_on:
                return None, "现在还没轮到这个座位（或这手已经过期）"
            if expected_move_count is not None and len(self.match.log) != int(
                expected_move_count
            ):
                return None, "局面已经变化（等待期间已有人落子），本手作废"
            before_state = self.match.state
            before_chat = len(self.match.chat)
            action: dict[str, Any] = {"type": "move", "move": move}
            if say and persist_say:
                action["say"] = say
            result = apply_match_action(self.match, seat, action, self.now_fn())
            if not result.ok:
                return None, result.message
            self.match = result.match
            saved = await self._persist_locked(force=True)
            self._refresh_status_locked()
            if saved:
                self._broadcast_locked()
            applied_say = ""
            if persist_say and len(self.match.chat) > before_chat:
                applied_say = str(self.match.chat[-1].text or "")
            event = {
                "type": "move",
                "seat": seat,
                "move": move,
                "say": applied_say,
                "over": status_of(self.match).outcome is not None,
                "match_id": self.match.id,
                "version": self.match.version,
                "match": self.match,
                "before_state": before_state,
                "delegated": bool(grant_key),
            }
            match = self.match
        if event is not None:
            self.notify_event(event)
        return match, ""

    async def set_mode_options(
        self, **changes: Any
    ) -> dict[str, Any]:
        """锁内更新模式选项（S3 difficulty / S4 delegation 切换）。

        语义约束：

        - 切换校验走 ``modes.validate_mode_switch``：与座位构成不兼容的
          模式/难度组合一律拒绝（抛 ``RoomError``，code=``bad_mode``）；
        - **省略项继承当前值**：只改 delegation（``set_mode_options(
          delegation=True)``）或 mode 不变时，保留当前 ``difficulty``，
          不会把 advanced 陪练局降级成 easy（那会因座位不兼容而 409）；
        - ``client_version`` 给出时在**同一把房间锁内**比对（与走子动作
          同口径的 ``stale_version`` 防护，页面防在过期视图上切换）；
        - 只改选项并持久化，**不 bump version**（不产生走子/页面失效），
          也不广播——委托/模式状态经 ``GET /matches/{id}/mode`` 读取；
        - 返回归一后的完整选项（dict）。
        """
        options_of = modes.options_of
        validate_mode_switch = modes.validate_mode_switch

        async with self.lock:
            if self.status == "over" or status_of(self.match).outcome is not None:
                raise RoomError("game_over", "对局已经结束，不能再切换模式")
            client_version = changes.pop("client_version", None)
            if (
                client_version is not None
                and int(client_version) != self.match.version
            ):
                raise RoomError("stale_version", "页面已过期，请刷新后重试")
            current = options_of(self.match)
            requested_level = changes.get("level", current.get("level", "intermediate"))
            if self.match.kind in ("gomoku", "reversi") and requested_level not in ("beginner", "intermediate", "advanced"):
                raise RoomError("bad_mode", "难度只能是初级、中级或高级")
            requested_mode = str(changes.get("mode") or current.get("mode") or "")
            requested_diff = str(changes.get("difficulty") or "").strip().lower()
            if not requested_diff:
                # delegation-only / 同模式 no-op：继承当前难度。
                requested_diff = str(current.get("difficulty") or "")
            normalized, error = validate_mode_switch(
                self.match, mode=requested_mode, difficulty=requested_diff
            )
            if normalized is None:
                raise RoomError("bad_mode", error or "模式切换不被允许")
            delegation = current.get("delegation")
            if "delegation" in changes:
                delegation = bool(changes.get("delegation"))
            merged = normalize_mode_options(
                {
                    **normalized,
                    "level": requested_level,
                    "delegation": delegation
                    and normalized.get("mode") == modes.MODE_BOT,
                },
                seats=self.match,
                kind=self.match.kind,
            )
            self.match = replace(self.match, mode_options=merged)
            await self._persist_locked(force=True)
            return dict(merged)

    async def disable_delegation(self, *, reason: str = "") -> bool:
        """抢救性关闭委托（模式/难度不动）：bot 兜底不可用时改回手动操作。

        只做内存+落库，不广播、不 bump version；用于 watchdog 发现当前游戏
        模块没有规则 bot、无法代走时，避免每一轮都重复铸造无解授权而永久
        卡住人类座位。返回是否真的从开启改成关闭。
        """
        options_of = modes.options_of

        async with self.lock:
            current = options_of(self.match)
            if not current.get("delegation"):
                return False
            self.match = replace(
                self.match,
                mode_options={**self.match.mode_options, "delegation": False},
            )
            await self._persist_locked(force=True)
            logger.warning(
                "委托自动关闭（%s）：%s", self.match.id, reason or "no bot fallback"
            )
            return True


    async def ai_step(self, seat: int) -> Match | None:
        """AI 一步（M1.5 规则 bot 兜底驱动）：锁内取招 → 应用 → 落库 → 广播。

        M2 的 ai_player 会替换 ``drive_ai_hook``，届时由 LLM 决定走子；这里
        先把「轮到 AI 却没人动」的死局补上。已结束/不在等待位/该游戏没有
        bot 时直接清理 ``ai_pending``，不产生副作用。
        """
        event: dict[str, Any] | None = None
        async with self.lock:
            if self.status == "over":
                self.ai_pending.discard(seat)
                return None
            status = status_of(self.match)
            module = games.get(self.match.kind)
            if (
                status.outcome is not None
                or seat not in status.waiting_on
                or type(module).bot is GameModule.bot
            ):
                self.ai_pending.discard(seat)
                return None
            move = module.bot(self.match.state, seat)
            if not move:
                self.ai_pending.discard(seat)
                return None
            before_state = self.match.state
            result = apply_match_action(
                self.match, seat, {"type": "move", "move": move}, self.now_fn()
            )
            if not result.ok:
                self.ai_pending.discard(seat)
                return None
            self.match = result.match
            saved = await self._persist_locked(force=True)
            self._refresh_status_locked()
            if saved:
                self._broadcast_locked()
            self.ai_pending.discard(seat)
            event = {
                "type": "move",
                "seat": seat,
                "move": move,
                "over": status_of(self.match).outcome is not None,
                "match_id": self.match.id,
                "version": self.match.version,
                "match": self.match,
                "before_state": before_state,
            }
            follow = self._check_ai_locked()
            match = self.match

        # AI 连走（多 AI 座位）时继续驱动；1v1 通常为空
        self._spawn_drive(follow)
        if event is not None:
            self.notify_event(event)
        return match

    async def kick_ai(self) -> None:
        """恢复/开局后补驱动：若当前轮到 AI，立即登记并起驱动任务。"""
        async with self.lock:
            seats = self._check_ai_locked()
        self._spawn_drive(seats)

    async def rebind_actor(
        self,
        actor: str,
        *,
        new_actor: str | None = None,
        name: str | None = None,
        session_id: str | None = None,
        source: str | None = None,
    ) -> bool:
        """改页面玩家在座位上的归属/显示名（身份设置或旧 ``webui`` 迁移）。

        只改匹配该 actor 的人端座位；不改口令、不终止对局。成功变更会
        bump version 并广播，页面上的座位条即时更新。``source`` 仅用于把
        历史未标注来源的页面局显式标成 ``webui``，聊天局不会被改。
        """
        async with self.lock:
            seats = list(self.match.seats)
            changed = False
            for i, seat in enumerate(seats):
                if not seat.actor or seat.actor != actor:
                    continue
                new_seat = replace(
                    seat,
                    actor=(new_actor if new_actor is not None else seat.actor),
                    name=name or seat.name,
                    session_id=session_id or seat.session_id,
                    source=(source or seat.source),
                )

                if new_seat != seat:
                    seats[i] = new_seat
                    changed = True
            if not changed:
                return False
            self.match = replace(
                self.match,
                seats=seats,
                version=self.match.version + 1,
                updated_at=self.now_fn(),
            )
            await self._persist_locked(force=True)
            self._broadcast_locked()
            return True

    async def flush(self) -> None:
        """强制落库（stop 前调用）；失败仍以内存为准并保持 dirty。"""
        async with self.lock:
            await self._persist_locked(force=True)

    # ------------------------------------------------------------------
    #  内部：状态 / 落库 / AI 触发
    # ------------------------------------------------------------------
    def _refresh_status_locked(self) -> None:
        self.status = "over" if status_of(self.match).outcome is not None else "active"

    async def _persist_locked(self, *, force: bool = False) -> bool:
        if not force and not self.dirty:
            return True
        status = "over" if status_of(self.match).outcome is not None else "active"
        try:
            await self.repository.save(self.match, status)
        except Exception:  # noqa: BLE001 - 落库失败不改变内存事实
            self.dirty = True
            logger.warning(
                "对局落库失败（内存为准，稍后补写）: %s", self.match.id, exc_info=True
            )
            return False
        self.dirty = False
        return True

    def _check_ai_locked(self) -> list[int]:
        """锁内 check-and-add：返回本次需要（重新）驱动的 AI 座位。"""
        if self.drive_ai_hook is None or self.status == "over":
            return []
        status = status_of(self.match)
        triggered: list[int] = []
        for seat in status.waiting_on:
            if self.match.seats[seat].kind != "ai":
                continue
            if seat in self.ai_pending:
                continue
            self.ai_pending.add(seat)
            triggered.append(seat)
        return triggered

    def _spawn_drive(self, seats: list[int]) -> None:
        if not seats or self.drive_ai_hook is None:
            return
        for seat in seats:
            generation = self._drive_gen.get(seat, 0) + 1
            self._drive_gen[seat] = generation
            task = asyncio.create_task(
                self._run_drive(seat, generation),
                name=f"gamehub-drive-{self.match.id}-{seat}",
            )
            self._tasks.add(task)
            self._drive_tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            task.add_done_callback(self._drive_tasks.discard)

    def cancel_drive_tasks(self) -> None:
        """取消在途 AI 决策任务（不等待、不持锁）。

        认输后调用：即使某个任务已错过取消窗口，``try_apply_ai`` 仍会在
        锁内重读状态并安全丢弃，不会落子到已结束的对局。调用方若是某个
        drive 任务自身，则跳过自己，避免自等待死锁。
        """
        current = asyncio.current_task()
        for task in list(self._drive_tasks):
            if task is not current and not task.done():
                task.cancel()
        self.ai_pending.clear()

    async def _run_drive(self, seat: int, generation: int = 0) -> None:
        try:
            hook = self.drive_ai_hook
            if hook is None:
                return
            await _maybe_await(hook(self, seat))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 驱动失败不影响对局
            logger.warning(
                "AI 驱动失败（%s seat %s）", self.match.id, seat, exc_info=True
            )
        finally:
            # 只有仍是本座位最新一次驱动时才清理在途标记；连走会排新代次。
            if self._drive_gen.get(seat, 0) <= generation:
                self.ai_pending.discard(seat)


class RoomManager:
    """对局注册表：创建/惰性恢复/按 actor 或 token 解析。"""

    def __init__(
        self,
        *,
        repository: Any,
        config: Any,
        now_fn: Callable[[], int] | None = None,
        opponent_name: str | None = None,
        bot_name: str | None = None,
        practice_name: str | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.now_fn = now_fn or _default_now
        # 进阶陪练座位的显示名（默认「陪练」，由上层传入实际名字）。
        self.opponent_name = (opponent_name or "").strip() or DEFAULT_OPPONENT_NAME
        # 宿主 bot 昵称（核心配置）；缺席时中性「对手」，不硬编码人设名。
        self.bot_name = (bot_name or "").strip() or "对手"
        # 规则陪练座位显示名（插件配置，默认「小白」）；多个陪练自动加序号。
        self.practice_name = (practice_name or "").strip() or DEFAULT_PRACTICE_NAME
        # M2 注入 ai_player 驱动；M1 保持 None。
        self.drive_ai_hook: Callable[[GameRoom, int], Any] | None = None
        # M1.5 注入 table_talk 事件消费；None=不发通知。
        self.event_hook: Callable[[GameRoom, dict[str, Any]], Any] | None = None

        self._rooms: dict[str, GameRoom] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    #  恢复 / 查询
    # ------------------------------------------------------------------
    async def restore(self) -> None:
        """启动惰性恢复：把仓储里的 active 对局装进内存 Map。

        上限来自实际配置（``max_active_matches``），不再写死仓储默认值；
        仓储 ``load_active`` 有 ``limit`` 形参时按配置传入，旧的零参假仓储
        仍兼容。恢复时按 actor 去重（DB 边缘：历史/并发可能留下同 actor
        多条 active，本应单 actor 单局）——按仓储的 updated_at 倒序只保留
        最新一条进内存；派生为终局的旧行（如认输行）直接跳过。
        """
        async with self._lock:
            loader = getattr(self.repository, "load_active", None)
            if not callable(loader):
                return
            limit = max(1, int(getattr(self.config, "max_active_matches", 200) or 200))
            rows: Any
            try:
                params = inspect.signature(loader).parameters
                wants_limit = "limit" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
            except (TypeError, ValueError):
                wants_limit = False
            try:
                if wants_limit:
                    rows = await _maybe_await(loader(limit=limit))
                else:
                    rows = await _maybe_await(loader())
            except Exception:  # noqa: BLE001 - 恢复失败不阻断热路径
                logger.warning("活跃对局恢复失败", exc_info=True)
                return
            seen_actors: set[str] = set()
            for row in rows or []:
                match = _as_match(row)
                if match is None or match.id in self._rooms:
                    continue
                try:
                    terminal = status_of(match).outcome is not None
                except Exception:  # noqa: BLE001 - 未知 kind/坏快照不阻断恢复
                    logger.warning(
                        "恢复时跳过无法派生状态的对局: %s", match.id, exc_info=True
                    )
                    continue
                if terminal:
                    # 旧版本可能把认输行写成 active；绝不复活成活跃局。
                    continue
                actors = {s.actor for s in match.seats if s.actor}
                if actors & seen_actors:
                    logger.warning(
                        "恢复时跳过同 actor 的旧 active 局: %s", match.id
                    )
                    continue
                seen_actors.update(actors)
                room = self._new_room(match)
                self._rooms[match.id] = room
                # 恢复后若正轮到 AI（进程重启前刚被驱动过），补上这一手
                await room.kick_ai()

    def get_room(self, match_id: str) -> GameRoom | None:
        return self._rooms.get(match_id)

    async def get_or_load(self, match_id: str) -> GameRoom | None:
        """内存未命中时惰性加载单局（GET/WS/工具首次访问）。"""
        room = self._rooms.get(match_id)
        if room is not None:
            return room
        async with self._lock:
            room = self._rooms.get(match_id)
            if room is not None:
                return room
            self.prune_rooms()
            match = await self._load_match(match_id)
            if match is None:
                return None
            room = self._new_room(match)
            self._rooms[match.id] = room
            return room

    async def _load_match(self, match_id: str) -> Match | None:
        loader = getattr(self.repository, "get", None)
        if not callable(loader):
            loader = getattr(self.repository, "load", None)
        try:
            if callable(loader):
                return _as_match(await _maybe_await(loader(match_id)))
            # 仓储没有单局读取时，退化为扫 active 列表。
            active_loader = getattr(self.repository, "load_active", None)
            if not callable(active_loader):
                return None
            for row in await _maybe_await(active_loader()) or []:
                match = _as_match(row)
                if match is not None and match.id == match_id:
                    return match
        except Exception:  # noqa: BLE001
            logger.warning("对局加载失败: %s", match_id, exc_info=True)
        return None

    def find_by_actor(self, actor: str) -> GameRoom | None:
        if not actor:
            return None
        for room in self._rooms.values():
            if room.status != "active":
                continue
            if any(s.actor and s.actor == actor for s in room.match.seats):
                return room
        return None

    def active_rooms(self) -> list[GameRoom]:
        return [room for room in self._rooms.values() if room.status == "active"]

    def count(self) -> int:
        """当前活跃局数（不含已结束但仍缓存的局）。"""
        return len(self.active_rooms())

    def set_bot_name(self, name: str | None) -> None:
        self.bot_name = (name or "").strip() or "对手"

    def set_opponent_name(self, name: str | None) -> None:
        """设置进阶陪练座位显示名（上层在加载配置后调用；空=保留默认）。"""
        self.opponent_name = (name or "").strip() or DEFAULT_OPPONENT_NAME

    def set_practice_name(self, name: str | None) -> None:
        """设置规则陪练座位显示名（插件配置 practice_name；空=保留默认「小白」）。"""
        self.practice_name = (name or "").strip() or DEFAULT_PRACTICE_NAME

    def prune_rooms(
        self,
        *,
        now: int | None = None,
        grace_s: float = _OVER_ROOM_GRACE_S,
        max_over: int = _MAX_OVER_ROOMS,
    ) -> list[str]:
        """清理内存里的终局房间（最小淘汰，不引入调度器）。

        - 只淘汰 ``status == "over"`` 且**没有客户端连接**的房间；
          有连接的终局房间保留，避免拆掉正在观看的页面。
        - 宽限从 ``updated_at`` 起算（默认 5 分钟），到期才删；迟到的
          GET/WS 仍会经 ``get_or_load`` 从仓储惰性恢复。
        - 额外上限 ``max_over``：宽限未到但终局房间过多时淘汰最旧的
          无连接房间，防止长运行进程无限积累。
        - 返回被移出内存的 match_id 列表。
        """
        now_ms = int(self.now_fn() if now is None else now)
        grace_ms = int(max(0.0, float(grace_s)) * 1000)
        over = [
            (match_id, room)
            for match_id, room in self._rooms.items()
            if room.status == "over"
        ]
        if not over:
            return []
        evicted: set[str] = set()
        for match_id, room in over:
            if room.sockets:
                continue
            age = now_ms - int(room.match.updated_at or 0)
            if age >= grace_ms:
                evicted.add(match_id)
        remaining = [
            (match_id, room)
            for match_id, room in over
            if match_id not in evicted
        ]
        if max_over >= 0 and len(remaining) > max_over:
            remaining.sort(key=lambda item: int(item[1].match.updated_at or 0))
            for match_id, room in remaining[: len(remaining) - max_over]:
                if not room.sockets:
                    evicted.add(match_id)
        for match_id in evicted:
            self._rooms.pop(match_id, None)
        return sorted(evicted)

    # ------------------------------------------------------------------
    #  创建
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        kind: str,
        actor: str,
        session_id: str,
        opponent: str = "ai",
        name: str | None = None,
        options: dict[str, Any] | None = None,
        players: int | None = None,
        source: str = "",
        now: int | None = None,
        mode_options: dict[str, Any] | None = None,
        opponent_name: str | None = None,
    ) -> tuple[GameRoom, int, str]:
        """开一局：单 actor 单局 + 全局上限；返回 (room, seat, token)。

        ``source`` 显式记录开局入口（``chat`` / ``webui``），随座位快照持久化；
        空=旧调用/测试，读取时兼容推断。路由解析优先用它，而不是比较
        session_id 是否恰好等于当前页面会话。

        ``mode_options``（S2-S4 模式选项）给出时按规范归一并**推导对手座位
        构成**（nori/advanced → ``ai`` 座位；easy → ``bot`` 座位），与
        ``opponent`` 参数二选一（同时给出且冲突时以 mode_options 为准）；
        缺省时保持 legacy 行为（按 ``opponent`` 参数定座位，读取时回填
        ``driver=agent`` 等），已有调用方与测试不受影响。

        ``opponent_name`` 是进阶陪练座位的显示名（由上层按 agent 配置传
        入）；缺省用 ``self.opponent_name``（默认「陪练」）。
        """
        async with self._lock:
            self.prune_rooms()
            if self.find_by_actor(actor) is not None:
                raise RoomError("actor_busy", "你已有一局未结束，先下完这局吧")
            if self.count() >= int(self.config.max_active_matches):
                raise RoomError("match_full", "对局数量已达上限，请稍后再开局")

            module = games.maybe_get(kind)
            if module is None:
                raise RoomError("bad_kind", f"未知游戏：{kind}")
            if not module.ready:
                raise RoomError("bad_kind", f"「{module.name['zh']}」暂不可用")

            total = int(players) if players is not None else module.players.default
            total = max(total, 1)
            normalized_mode: dict[str, Any] | None = None
            advanced_name: str | None = None
            if kind == "doudizhu":
                if total != 3:
                    raise RoomError("bad_request", "斗地主固定三个席位")
                try:
                    normalized_mode = normalize_mode_options(mode_options, kind=kind)
                except ValueError as exc:
                    raise RoomError("bad_request", str(exc)) from exc
            elif mode_options is not None:
                probe = normalize_mode_options(mode_options, kind=kind)
                if probe["mode"] == modes.MODE_BOT and probe["difficulty"] == modes.DIFF_EASY:
                    opponent = "bot"
                else:
                    opponent = "ai"
                if opponent not in ("ai", "bot"):
                    raise RoomError("bad_opponent", f"对手只能是{self.bot_name}本人或规则陪练")
                if opponent == "bot" and not _has_bot(module):
                    raise RoomError("bad_opponent", f"{module.name['zh']}没有规则陪练座位")
                normalized_mode = normalize_mode_options(
                    probe,
                    seats=_probe_seats(opponent, total),
                    kind=kind,
                )
                if (
                    normalized_mode["mode"] == modes.MODE_BOT
                    and normalized_mode["difficulty"] == modes.DIFF_ADVANCED
                ):
                    # 队友局（S3 advanced）：ai 座位是「进阶陪练」对手而非宿主主 agent
                    # 本人——座位显示名由上层配置注入（默认「陪练」），
                    # 文本/对局视图/历史/通知全链路一致，避免 UI 与后端
                    # 的身份口径分裂。
                    advanced_name = (
                        (opponent_name or "").strip() or self.opponent_name
                    )
            else:
                if opponent not in ("ai", "bot"):
                    raise RoomError("bad_opponent", f"对手只能是{self.bot_name}本人或规则陪练")
                if opponent == "bot" and not _has_bot(module):
                    raise RoomError("bad_opponent", f"{module.name['zh']}没有规则陪练座位")

            token = secrets.token_hex(16)
            seats: list[NewSeat] = [
                NewSeat(
                    kind="human",
                    token=token,
                    name=name,
                    joined=True,
                    actor=actor,
                    session_id=session_id,
                    source=source,
                )
            ]
            for index in range(1, total):
                if kind == "doudizhu":
                    driver = normalized_mode["seat_drivers"][index]
                    seat_kind = "bot" if driver == "bot" else "ai"
                    if driver == "turn":
                        seat_name = self.bot_name
                    elif driver == "agent":
                        seat_name = (opponent_name or "").strip() or self.opponent_name
                    else:
                        seat_name = None
                    seats.append(NewSeat(kind=seat_kind, token="", name=seat_name, joined=True))
                    continue
                seat_kind = opponent if index == 1 else "ai"
                if index == 1 and advanced_name:
                    seats.append(NewSeat(kind=seat_kind, token="", name=advanced_name))
                else:
                    seats.append(NewSeat(kind=seat_kind, token=""))

            match_id = self._new_match_id_locked()
            when = self.now_fn() if now is None else int(now)
            try:
                match = create_match(
                    NewMatchOptions(
                        id=match_id,
                        kind=kind,
                        seats=seats,
                        seed=secrets.randbits(32),
                        now=when,
                        options=dict(options or {}),
                        mode_options=normalized_mode or {},
                        bot_name=self.bot_name,
                        practice_name=self.practice_name,
                    )
                )
            except (ValueError, KeyError) as exc:
                raise RoomError("bad_request", str(exc) or "开局参数不合法") from exc

            room = self._new_room(match)
            self._rooms[match.id] = room
            async with room.lock:
                await room._persist_locked(force=True)
                drive = room._check_ai_locked()
            room._spawn_drive(drive)
            room.notify_event(
                {
                    "type": "start",
                    "seat": 0,
                    "match_id": room.match.id,
                    "version": room.match.version,
                    "match": room.match,
                }
            )
            return room, 0, token

    def _new_match_id_locked(self) -> str:
        while True:
            match_id = secrets.token_hex(8)
            if match_id not in self._rooms:
                return match_id

    def _new_room(self, match: Match) -> GameRoom:
        # 传稳定代理而不是当前 hook 值：hook 可在 RoomManager 上后置注入/替换
        # （M1.5 bot 兜底 → M2 ai_player），已建房间也随即可用。
        room = GameRoom(
            match,
            repository=self.repository,
            now_fn=self.now_fn,
            drive_ai_hook=self._dispatch_drive,
        )
        room.event_hook = self._dispatch_event
        return room

    async def _dispatch_drive(self, room: GameRoom, seat: int) -> None:
        hook = self.drive_ai_hook
        if hook is None:
            return
        await _maybe_await(hook(room, seat))

    async def _dispatch_event(self, room: GameRoom, event: dict[str, Any]) -> None:
        hook = self.event_hook
        if hook is None:
            return
        await _maybe_await(hook(room, event))

    # ------------------------------------------------------------------
    #  按 token / actor 提交
    # ------------------------------------------------------------------
    async def apply_for_token(
        self,
        match_id: str,
        token: str,
        action: dict[str, Any],
        *,
        client_version: int | None = None,
    ) -> Match:
        room = await self.get_or_load(match_id)
        if room is None:
            raise RoomError("not_found", "找不到这局")
        seat = seat_by_token(room.match, token)
        if seat is None:
            raise RoomError("bad_token", "口令不对，无法操作这局")
        return await room.apply(seat, action, client_version=client_version)

    async def apply_for_actor(
        self,
        actor: str,
        action: dict[str, Any],
        *,
        client_version: int | None = None,
    ) -> Match:
        room = self.find_by_actor(actor)
        if room is None:
            raise RoomError("not_found", "找不到进行中的对局")
        seat = next(
            (i for i, s in enumerate(room.match.seats) if s.actor and s.actor == actor),
            None,
        )
        if seat is None:
            raise RoomError("not_found", "找不到你的座位")
        return await room.apply(seat, action, client_version=client_version)

    async def resign_for_actor(self, actor: str) -> Match:
        return await self.apply_for_actor(actor, {"type": "resign"})

    async def resign_ai(self, actor: str) -> Match:
        """bot 认输：只认当前局 bot 本人座位（``kind == "ai"`` 且非队友局）。

        不代用户（human）或规则陪练（bot）认输；没有 AI 座位时给明确错误。
        队友模式（S3/S4）里 ``ai`` 座位（如有）是**对手**（advanced 陪练），
        宿主主 agent 自己没有座位——同样拒绝，绝不误让对方座位认输。
        认输仍走房间锁（``GameRoom.apply``），结束后取消/丢弃在途任务，
        避免“认输后又落一子”。
        """
        room = self.find_by_actor(actor)
        if room is None:
            raise RoomError("not_found", "你现在没有进行中的对局")
        if modes.role_of(room.match) == modes.ROLE_TEAMMATE:
            raise RoomError(
                "no_ai_seat",
                f"当前对局里没有{self.bot_name}的座位（她是你的队友，不占座位），"
                f"不能由{self.bot_name}认输",
            )
        seat = modes.nori_seat_index(room.match)
        if seat is None:
            raise RoomError(
                "no_ai_seat",
                f"当前对局里没有{self.bot_name}的座位（对手是规则陪练或真人），"
                f"不能由{self.bot_name}认输",
            )
        try:
            return await room.apply(seat, {"type": "resign"})
        finally:
            room.cancel_drive_tasks()

    async def flush_all(self) -> None:
        rooms = list(self._rooms.values())
        if rooms:
            await asyncio.gather(*(room.flush() for room in rooms), return_exceptions=True)


def _as_match(row: Any) -> Match | None:
    if isinstance(row, Match):
        return row
    if isinstance(row, dict):
        try:
            return Match.from_dict(row)
        except (KeyError, TypeError, ValueError):
            return None
    return None
