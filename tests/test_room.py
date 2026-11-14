"""room.py 单元测试：单局/口令/版本/落库/广播/并发/关闭（设计 §5.3）。

仓储用本地假实现（记录 save 调用、可注入失败），不连真实 DB；
``Settings`` 提供 nori ``GamehubConfig`` 的属性式配置视图。
``test_room_works_with_match_store`` 另用 ``host.store.MatchStore``（tmp_path）
验证真实仓储契约（save/get/load_active）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from engine.match import Match
from host.settings import Settings
from host.store import MatchStore
from room import GameRoom, RoomError, RoomManager


class FakeRepo:
    """内存仓储：save 记录 (match_id, version, status)，可切换失败。"""

    def __init__(self) -> None:
        self.saves: list[tuple[str, int, str]] = []
        self.snapshots: dict[str, Match] = {}
        self.statuses: dict[str, str] = {}
        self.fail = False

    async def save(self, match: Match, status: str) -> None:
        if self.fail:
            raise RuntimeError("db unavailable")
        self.saves.append((match.id, match.version, status))
        self.snapshots[match.id] = Match.from_dict(match.to_dict())
        self.statuses[match.id] = status

    async def load(self, match_id: str) -> Match | None:
        snapshot = self.snapshots.get(match_id)
        return Match.from_dict(snapshot.to_dict()) if snapshot is not None else None

    async def load_active(self) -> list[Match]:
        return [
            Match.from_dict(snapshot.to_dict())
            for match_id, snapshot in self.snapshots.items()
            if self.statuses.get(match_id) == "active"
        ]


class FakeWS:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.closed: tuple[int, bytes] | None = None

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.messages.append(payload)

    async def close(self, code: int = 1000, message: bytes = b"") -> None:
        self.closed = (code, message)


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def make_manager(
    repo: FakeRepo | None = None, *, max_active: int = 10, now: int = 1000
) -> RoomManager:
    config = Settings({"max_active_matches": max_active})
    return RoomManager(
        repository=repo or FakeRepo(),
        config=config,
        now_fn=lambda: now,
        bot_name="诺里",
    )


async def create_gomoku(
    manager: RoomManager, actor: str = "alice", **kwargs: Any
) -> tuple[GameRoom, int, str]:
    return await manager.create(
        kind="gomoku",
        actor=actor,
        session_id=f"session-{actor}",
        name=kwargs.pop("name", actor),
        **kwargs,
    )


# ----------------------------------------------------------------------
#  创建
# ----------------------------------------------------------------------
async def test_create_tokens_and_seats() -> None:
    repo = FakeRepo()
    manager = make_manager(repo)
    room, seat, token = await create_gomoku(manager)

    assert seat == 0
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    assert room.match.seats[0].token == token
    assert room.match.seats[0].kind == "human"
    assert room.match.seats[0].actor == "alice"
    assert room.match.seats[0].session_id == "session-alice"
    assert room.match.seats[1].kind == "ai"
    assert room.match.seats[1].token == ""
    assert len(room.match.id) == 16
    assert room.status == "active"
    assert manager.count() == 1
    assert manager.get_room(room.match.id) is room
    # 首帧已落库 active
    assert repo.saves == [(room.match.id, 1, "active")]

    # 公开摘要与视图都不含口令
    public = room.public_state()
    assert "token" not in json.dumps(public, ensure_ascii=False)
    assert "token" not in json.dumps(room.view(0), ensure_ascii=False)


async def test_create_bot_seat_has_no_token() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(manager, opponent="bot")
    assert room.match.seats[1].kind == "bot"
    assert room.match.seats[1].token == ""
    assert room.match.seats[1].name  # bot 有默认名


async def test_create_actor_busy_and_match_full() -> None:
    manager = make_manager(max_active=2)
    await create_gomoku(manager, "alice")

    with pytest.raises(RoomError) as busy:
        await create_gomoku(manager, "alice")
    assert busy.value.code == "actor_busy"

    await create_gomoku(manager, "bob")
    with pytest.raises(RoomError) as full:
        await create_gomoku(manager, "carol")
    assert full.value.code == "match_full"
    assert manager.count() == 2


async def test_create_bad_kind_and_opponent() -> None:
    manager = make_manager()
    with pytest.raises(RoomError) as bad_kind:
        await manager.create(kind="nope", actor="a", session_id="s")
    assert bad_kind.value.code == "bad_kind"

    with pytest.raises(RoomError) as bad_opponent:
        await manager.create(
            kind="gomoku", actor="a", session_id="s", opponent="human"
        )
    assert bad_opponent.value.code == "bad_opponent"


# ----------------------------------------------------------------------
#  口令 / 版本 / 终局
# ----------------------------------------------------------------------
async def test_apply_for_token_and_bad_token() -> None:
    manager = make_manager()
    room, _, token = await create_gomoku(manager)
    match_id = room.match.id

    updated = await manager.apply_for_token(
        match_id, token, {"type": "move", "move": "H8"}, client_version=1
    )
    assert updated.version == 2
    assert updated.state["moves"] == 1
    assert updated.state.get("last") is not None

    with pytest.raises(RoomError) as bad:
        await manager.apply_for_token(match_id, "0" * 32, {"type": "move", "move": "J9"})
    assert bad.value.code == "bad_token"
    assert bad.value.message  # 中文短句可直接给用户

    with pytest.raises(RoomError) as missing:
        await manager.apply_for_token("f" * 16, token, {"type": "move", "move": "J9"})
    assert missing.value.code == "not_found"


async def test_client_version_conflict() -> None:
    manager = make_manager()
    room, _, token = await create_gomoku(manager)
    match_id = room.match.id

    await manager.apply_for_token(
        match_id, token, {"type": "move", "move": "H8"}, client_version=1
    )
    with pytest.raises(RoomError) as stale:
        await manager.apply_for_token(
            match_id, token, {"type": "move", "move": "J8"}, client_version=1
        )
    assert stale.value.code == "stale_version"

    # AI/内部路径不带 version：改名动作可直接应用。
    renamed = await manager.apply_for_token(match_id, token, {"type": "rename", "name": "新名"})
    assert renamed.version == 3
    assert renamed.seats[0].name == "新名"


async def test_game_over_rejects_move_and_releases_actor() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(manager)
    match_id = room.match.id

    over = await manager.resign_for_actor("alice")
    assert room.status == "over"
    assert over.resigned == 0
    assert room.public_state()["over"] is True
    assert manager.find_by_actor("alice") is None
    assert manager.active_rooms() == []
    assert manager.count() == 0

    with pytest.raises(RoomError) as ended:
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=over.version)
    assert ended.value.code == "game_over"

    # 已结束的局不再占用单 actor 单局名额
    room2, _, _ = await create_gomoku(manager, "alice")
    assert room2.match.id != match_id


async def test_find_by_actor() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(manager, "alice")
    assert manager.find_by_actor("alice") is room
    assert manager.find_by_actor("bob") is None
    assert manager.find_by_actor("") is None


# ----------------------------------------------------------------------
#  落库
# ----------------------------------------------------------------------
async def test_save_failure_marks_dirty_then_flush_rewrites() -> None:
    repo = FakeRepo()
    manager = make_manager(repo)
    room, _, token = await create_gomoku(manager)
    assert room.dirty is False
    saves_before = len(repo.saves)

    ws = FakeWS()
    await room.attach(ws, 0)
    await wait_until(lambda: len(ws.messages) >= 1)
    ws.messages.clear()

    repo.fail = True
    updated = await room.apply(0, {"type": "rename", "name": "甲"}, client_version=1)
    assert updated.version == 2
    assert room.dirty is True
    assert len(repo.saves) == saves_before  # 没有新的成功落库
    await asyncio.sleep(0.05)
    assert ws.messages == []  # 落库失败不广播

    repo.fail = False
    await room.flush()
    assert room.dirty is False
    assert repo.saves[-1] == (room.match.id, 2, "active")
    assert repo.snapshots[room.match.id].seats[0].name == "甲"

    # 下一次正常变更整行补写（last-write-wins）
    await room.apply(0, {"type": "rename", "name": "乙"}, client_version=2)
    assert repo.saves[-1] == (room.match.id, 3, "active")
    assert repo.snapshots[room.match.id].seats[0].name == "乙"
    await room.detach(ws)


async def test_flush_all_and_status_over_saved() -> None:
    repo = FakeRepo()
    manager = make_manager(repo)
    room_a, _, _ = await create_gomoku(manager, "alice")
    room_b, _, _ = await create_gomoku(manager, "bob")

    repo.fail = True
    await room_a.apply(0, {"type": "rename", "name": "甲"}, client_version=1)
    await room_b.apply(0, {"type": "resign"})
    assert room_a.dirty is True

    repo.fail = False
    await manager.flush_all()
    assert room_a.dirty is False
    assert repo.statuses[room_a.match.id] == "active"
    assert repo.statuses[room_b.match.id] == "over"


# ----------------------------------------------------------------------
#  并发
# ----------------------------------------------------------------------
async def test_concurrent_apply_with_same_version_only_one_wins() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(manager)
    version = room.match.version

    async def act(move: str):
        try:
            return await room.apply(0, {"type": "move", "move": move}, client_version=version)
        except RoomError as exc:  # noqa: PERF203 - 测试内逐协程捕获
            return exc

    results = await asyncio.gather(act("H8"), act("J8"))
    matches = [r for r in results if isinstance(r, Match)]
    errors = [r for r in results if isinstance(r, RoomError)]
    assert len(matches) == 1
    assert len(errors) == 1
    assert errors[0].code == "stale_version"
    assert room.match.version == 2


async def test_concurrent_apply_without_version_serializes() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(manager)

    async def act(move: str):
        try:
            return await room.apply(0, {"type": "move", "move": move})
        except RoomError as exc:  # noqa: PERF203
            return exc

    results = await asyncio.gather(act("H8"), act("J8"))
    assert sum(isinstance(r, Match) for r in results) == 1
    errors = [r for r in results if isinstance(r, RoomError)]
    assert len(errors) == 1
    assert errors[0].code == "illegal"  # 第二手不是同座位的回合
    assert room.match.version == 2


# ----------------------------------------------------------------------
#  AI 路径 / ai_pending
# ----------------------------------------------------------------------
async def test_try_apply_ai_rereads_status_inside_lock() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(manager)

    # 当前等待 human，AI 座位走子直接丢弃
    assert await room.try_apply_ai(1, "H8") is None
    assert room.match.version == 1

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    assert room.match.version == 2
    assert room.match.state["moves"] == 1

    applied = await room.try_apply_ai(1, "J9")
    assert applied is not None
    assert applied.version == 3
    assert applied.state["moves"] == 2
    assert room.ai_pending == set()

    await room.apply(0, {"type": "resign"}, client_version=3)
    assert await room.try_apply_ai(0, "J8") is None
    assert room.status == "over"


async def test_drive_hook_and_ai_pending_invariant() -> None:
    manager = make_manager()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def hook(room: GameRoom, seat: int) -> None:
        calls.append(seat)
        started.set()
        await release.wait()
        await room.try_apply_ai(seat, "J9")

    manager.drive_ai_hook = hook
    room, _, _ = await create_gomoku(manager)

    try:
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert room.ai_pending == {1}

        # 等待期间再变更：check-and-add 不应重复登记/重复驱动
        await room.apply(
            0, {"type": "rename", "name": "甲"}, client_version=room.match.version
        )
        assert room.ai_pending == {1}
        assert calls == [1]
    finally:
        release.set()

    await wait_until(lambda: not room.ai_pending and room.match.state["moves"] == 2)
    assert calls == [1]
    assert room.match.version == 4


async def test_ai_step_plays_rule_bot_move() -> None:
    """ai_step：轮不到 AI 时无副作用；轮到 AI 时锁内取招、推进版本并清 pending。"""
    manager = make_manager()
    room, _, _ = await create_gomoku(manager)

    # 仍轮到 human：AI 步直接丢弃
    assert await room.ai_step(1) is None
    assert room.match.version == 1
    assert room.ai_pending == set()

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    assert room.match.state["moves"] == 1

    applied = await room.ai_step(1)
    assert applied is not None
    assert applied.version == 3
    assert applied.state["moves"] == 2
    assert applied.log[-1].seat == 1
    assert room.ai_pending == set()


async def test_ai_step_noop_after_game_over() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(manager)
    await room.apply(0, {"type": "resign"}, client_version=1)
    assert room.status == "over"
    assert await room.ai_step(1) is None
    assert room.match.version == 2


# ----------------------------------------------------------------------
#  广播 / WS
# ----------------------------------------------------------------------
async def test_broadcast_per_socket_ordered_and_no_token() -> None:
    manager = make_manager()
    room, _, token = await create_gomoku(manager)
    seat_ws = FakeWS()
    spectator_ws = FakeWS()
    await room.attach(seat_ws, 0)
    await room.attach(spectator_ws, None)
    await wait_until(
        lambda: len(seat_ws.messages) >= 1 and len(spectator_ws.messages) >= 1
    )
    seat_ws.messages.clear()
    spectator_ws.messages.clear()

    await room.apply(0, {"type": "rename", "name": "甲"}, client_version=1)
    await room.apply(0, {"type": "rename", "name": "乙"}, client_version=2)
    await wait_until(
        lambda: len(seat_ws.messages) >= 2 and len(spectator_ws.messages) >= 2
    )

    versions = [m["match"]["version"] for m in seat_ws.messages[:2]]
    assert versions == [2, 3]  # per-socket 保序
    assert seat_ws.messages[0]["match"]["me"] == 0
    assert seat_ws.messages[0]["match"]["view"]["you"] == 1
    assert spectator_ws.messages[0]["match"]["me"] is None
    assert spectator_ws.messages[0]["match"]["view"]["you"] is None

    for message in [*seat_ws.messages[:2], *spectator_ws.messages[:2]]:
        assert message["type"] == "match"
        text = json.dumps(message, ensure_ascii=False)
        assert token not in text
        assert "token" not in text

    await room.detach(seat_ws)
    await room.detach(spectator_ws)
    assert room.sockets == {}


async def test_attach_replaces_existing_socket_and_close_all() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(manager)
    first = FakeWS()
    await room.attach(first, 0)
    # 同一 socket 重复 attach 只保留一个发送任务
    await room.attach(first, 0)
    await wait_until(lambda: len(first.messages) >= 1)

    second = FakeWS()
    await room.attach(second, None)
    await room.close_all()
    assert room.sockets == {}
    assert room._writers == {}
    assert first.closed is not None
    assert second.closed is not None
    assert first.closed[0] == 4003
    assert first.closed[1] == b"plugin stopping"


# ----------------------------------------------------------------------
#  恢复 / 惰性加载
# ----------------------------------------------------------------------
async def test_restore_and_lazy_load() -> None:
    repo = FakeRepo()
    manager = make_manager(repo)
    room, _, token = await create_gomoku(manager, "alice")
    match_id = room.match.id
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)

    lazy_manager = make_manager(repo)
    assert lazy_manager.get_room(match_id) is None
    loaded = await lazy_manager.get_or_load(match_id)
    assert loaded is not None
    assert loaded.match.seats[0].token == token
    assert loaded.match.version == 2
    assert lazy_manager.count() == 1

    restored_manager = make_manager(repo)
    await restored_manager.restore()
    restored = restored_manager.get_room(match_id)
    assert restored is not None
    assert restored.status == "active"
    assert restored_manager.find_by_actor("alice") is restored

    # 恢复后的房间仍可按口令提交（改名不依赖回合）
    updated = await restored_manager.apply_for_token(
        match_id, token, {"type": "rename", "name": "恢复后"}
    )
    assert updated.version == 3
    assert updated.seats[0].name == "恢复后"


async def test_restore_skips_duplicates_and_missing_repo_methods() -> None:
    class BareRepo:
        pass

    manager = make_manager(BareRepo())  # type: ignore[arg-type]
    await manager.restore()  # 无 load_active：静默返回
    assert manager.get_room("x") is None
    assert await manager.get_or_load("x") is None

    repo = FakeRepo()
    real = make_manager(repo)
    room, _, _ = await create_gomoku(real, "alice")
    restored = make_manager(repo)
    await restored.restore()
    await restored.restore()
    assert restored.count() == 1
    assert restored.get_room(room.match.id) is not None

async def test_restore_derives_resigned_and_skips_phantom_active() -> None:
    """重启恢复：旧行 status=active 但日志有认输 → 不复活、不占 actor 名额。"""
    repo = FakeRepo()
    manager = make_manager(repo)
    room, _, token = await create_gomoku(manager, "alice")
    await manager.resign_for_actor("alice")
    assert repo.statuses[room.match.id] == "over"

    # 模拟旧版本留下的坏行：状态列被写回 active。
    repo.statuses[room.match.id] = "active"

    restarted = make_manager(repo)
    await restarted.restore()
    assert restarted.get_room(room.match.id) is None  # 不复活
    assert restarted.count() == 0
    assert restarted.find_by_actor("alice") is None

    # 单局仍可惰性加载（终局视图），状态为 over。
    loaded = await restarted.get_or_load(room.match.id)
    assert loaded is not None and loaded.status == "over"
    assert loaded.match.resigned == 0

    # 同一 actor 可以正常开新局
    room2, _, _ = await create_gomoku(restarted, "alice")
    assert room2.match.id != room.match.id
    assert token  # 旧口令仍有效但只读视图


async def test_set_mode_options_delegation_only_preserves_advanced_difficulty() -> None:
    """delegation-only / 同模式 no-op 不降级当前 advanced 难度。"""
    manager = make_manager()
    room, _, token = await create_gomoku(
        manager,
        "alice",
        mode_options={"mode": "bot", "difficulty": "advanced"},
    )
    assert room.match.seats[1].kind == "ai"

    merged = await room.set_mode_options(delegation=True)
    assert merged["mode"] == "bot"
    assert merged["driver"] == "agent"
    assert merged["difficulty"] == "advanced"  # 不因省略 difficulty 变 easy
    assert merged["delegation"] is True

    # 只切 mode（同值 no-op）：仍保留 advanced。
    merged = await room.set_mode_options(mode="bot", delegation=False)
    assert merged["difficulty"] == "advanced"
    assert merged["delegation"] is False

    # 显式切回 nori 仍然生效（难度归 standard）。
    merged = await room.set_mode_options(mode="nori", difficulty="")
    assert merged["mode"] == "nori"
    assert merged["driver"] == "turn"


async def test_ai_seat_name_comes_from_core_bot_name() -> None:
    """AI 座位默认名读宿主 bot 名，不在插件里硬编码人设名。"""
    manager = make_manager()
    manager.set_bot_name("阿空")
    room, _, _ = await create_gomoku(manager, "alice")
    assert room.match.seats[1].kind == "ai"
    assert room.match.seats[1].name == "阿空"
    assert "诺里" not in {s.name for s in room.match.seats}


async def test_create_accepts_configured_opponent_name() -> None:
    manager = make_manager()
    manager.set_opponent_name("阿练")
    room, _, _ = await create_gomoku(
        manager, "alice", mode_options={"mode": "bot", "difficulty": "advanced"}
    )
    assert room.match.seats[1].name == "阿练"

    room2, _, _ = await create_gomoku(
        manager,
        "bob",
        mode_options={"mode": "bot", "difficulty": "advanced"},
        opponent_name="自定义陪练",
    )
    assert room2.match.seats[1].name == "自定义陪练"
    # 默认 nori 局不受影响
    room3, _, _ = await create_gomoku(manager, "carol")
    assert room3.match.seats[1].name == "诺里"


async def test_atomic_move_say_only_on_success_and_single_version() -> None:
    """room 原子 move+say：成功才进 chat、只 bump 一次版本；失败/迟到不写。"""
    manager = make_manager()
    room, _, _ = await create_gomoku(manager)
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    version = room.match.version
    chat_before = len(room.match.chat)

    # 非法落子 + say：say 绝不落库，版本不变。
    failed, error = await room.apply_ai_move(1, "ZZ99", say="这手不算")
    assert failed is None and error
    assert len(room.match.chat) == chat_before
    assert room.match.version == version

    # 迟到 expected_move_count + say：同样不写。
    stale, _ = await room.apply_ai_move(
        1, "J9", expected_move_count=0, say="迟到的话"
    )
    assert stale is None
    assert len(room.match.chat) == chat_before

    # 成功：chat 与落子同一次版本推进。
    events: list[dict[str, Any]] = []
    room.event_hook = lambda _room, event: events.append(dict(event))
    moved, error = await room.apply_ai_move(1, "J9", say="好棋")
    assert moved is not None and error == ""
    assert moved.version == version + 1
    assert len(moved.chat) == chat_before + 1
    assert moved.chat[-1].text == "好棋" and moved.chat[-1].seat == 1
    assert moved.chat[-1].at == len(moved.log)
    assert moved.chat[-1].by == ""  # AI 席发言归属座位本人
    await asyncio.sleep(0)
    assert events and events[-1]["say"] == "好棋"


async def test_delegated_atomic_say_and_stale_guard() -> None:
    manager = make_manager()
    room, _, _ = await create_gomoku(
        manager, "alice", mode_options={"mode": "bot", "difficulty": "easy"}
    )
    # 人类先手：代走 + say 原子生效（easy 陪练应手内嵌）
    moved, error = await room.apply_delegated_move(
        0, "H8", expected_move_count=0, say="我下这里"
    )
    assert moved is not None and error == ""
    assert moved.chat[-1].text == "我下这里"
    assert moved.chat[-1].by == "host"  # 座位是人类座位，说话的是宿主主 agent
    assert [e.seat for e in moved.log] == [0, 1]

    # 旧手数 + say：作废且不写 chat。
    before = len(room.match.chat)
    failed, _ = await room.apply_delegated_move(
        0, "J8", expected_move_count=0, say="过期的话"
    )
    assert failed is None
    assert len(room.match.chat) == before


async def test_delegated_move_drives_the_waiting_agent_seat() -> None:
    """代打落子后必须重新登记 AI 驱动：对手 agent 席（kind=ai）要被唤醒。

    回归背景：apply_delegated_move 原本只广播事件、从不 _spawn_drive——
    队友代打模式下宿主主 AI 替人类席落子后，agent 驱动的对手席永远等
    不到下一次决策任务，对局卡死（手动走子路径 apply 会唤醒，代打不会）。
    """
    manager = make_manager()
    calls: list[int] = []

    async def hook(room: GameRoom, seat: int) -> None:
        calls.append(seat)

    manager.drive_ai_hook = hook
    room, _, _ = await create_gomoku(manager, "alice")

    # 引擎绝不自动步进 kind=ai 座位（陪练 easy 的 bot 席才会内嵌应手），
    # 代打后轮到它时只能靠 drive 唤醒。
    assert room.match.seats[1].kind == "ai"
    moved, error = await room.apply_delegated_move(0, "H8", expected_move_count=0)
    assert moved is not None and error == ""
    assert [e.seat for e in moved.log] == [0]  # 对手席未被引擎代走

    await wait_until(lambda: calls == [1])

    # 失败的代打（过期手数）不得误触发驱动。
    calls.clear()
    failed, _ = await room.apply_delegated_move(
        0, "J9", expected_move_count=0
    )
    assert failed is None
    await asyncio.sleep(0)
    assert calls == []


async def test_prune_rooms_evicts_over_rooms_but_keeps_connected_and_active() -> None:
    repo = FakeRepo()
    manager = make_manager(repo, now=1000)
    over_room, _, _ = await create_gomoku(manager, "alice")
    await manager.resign_for_actor("alice")
    active_room, _, _ = await create_gomoku(manager, "bob")

    # 宽限内不清理
    assert manager.prune_rooms(now=1000, grace_s=300) == []
    # 有客户端连接时即使过期也保留（不拆正在观看的页面）
    ws = FakeWS()
    await over_room.attach(ws, None)
    assert manager.prune_rooms(now=1000 + 301_000, grace_s=300) == []
    await over_room.detach(ws)
    # 无连接 + 超过宽限 → 淘汰；活跃局不受影响
    evicted = manager.prune_rooms(now=1000 + 301_000, grace_s=300)
    assert evicted == [over_room.match.id]
    assert manager.get_room(over_room.match.id) is None
    assert manager.get_room(active_room.match.id) is active_room
    # 迟到访问仍可从仓储惰性加载（终局视图）
    reloaded = await manager.get_or_load(over_room.match.id)
    assert reloaded is not None and reloaded.status == "over"


async def test_restore_uses_config_limit_and_dedupes_actor() -> None:
    from engine.match import NewMatchOptions, NewSeat, create_match

    class LimitRepo(FakeRepo):
        def __init__(self) -> None:
            super().__init__()
            self.limits: list[int] = []

        async def load_active(self, limit: int = 200):
            self.limits.append(limit)
            rows = await super().load_active()
            return sorted(rows, key=lambda m: m.updated_at, reverse=True)

    repo = LimitRepo()
    # 两个同 actor 的 active 快照（DB 历史边缘）：只恢复更新的一个。
    old = create_match(
        NewMatchOptions(
            id="old-row",
            kind="gomoku",
            seats=[NewSeat(kind="human", token="t-old", actor="alice"), NewSeat(kind="ai", token="")],
            seed=1,
            now=100,
        )
    )
    new = create_match(
        NewMatchOptions(
            id="new-row",
            kind="gomoku",
            seats=[NewSeat(kind="human", token="t-new", actor="alice"), NewSeat(kind="ai", token="")],
            seed=2,
            now=200,
        )
    )
    repo.snapshots["old-row"] = old
    repo.snapshots["new-row"] = new
    repo.statuses["old-row"] = "active"
    repo.statuses["new-row"] = "active"

    manager = make_manager(repo, max_active=2, now=1000)
    await manager.restore()
    assert repo.limits == [2]
    assert manager.get_room("new-row") is not None
    assert manager.get_room("old-row") is None
    assert manager.count() == 1


async def test_restore_skips_rows_with_unknown_kind() -> None:
    """仓储里有未知游戏/坏快照时，恢复不得整体失败或复活幻影局。"""
    repo = FakeRepo()
    from engine.match import NewMatchOptions, NewSeat, create_match

    good = create_match(
        NewMatchOptions(
            id="good-row",
            kind="gomoku",
            seats=[NewSeat(kind="human", token="t", actor="alice"), NewSeat(kind="ai", token="")],
            seed=1,
            now=100,
        )
    )
    phantom = Match(
        id="unknown-row",
        kind="chess",
        created_at=100,
        updated_at=100,
        version=1,
        seats=[],
        state={},
    )
    repo.snapshots["good-row"] = good
    repo.snapshots["unknown-row"] = phantom
    repo.statuses["good-row"] = "active"
    repo.statuses["unknown-row"] = "active"

    manager = make_manager(repo)
    await manager.restore()
    assert manager.get_room("good-row") is not None
    assert manager.get_room("unknown-row") is None


async def test_event_hook_gets_start_move_and_resign() -> None:
    """table_talk 事件：开局、落子、认输各派发一次（终局标记 over）。"""
    manager = make_manager()
    events: list[tuple[str, dict[str, Any]]] = []

    async def hook(room: GameRoom, event: dict[str, Any]) -> None:
        events.append((room.match.id, dict(event)))

    manager.event_hook = hook
    room, seat, token = await create_gomoku(manager, opponent="ai")
    await wait_until(lambda: any(e[1].get("type") == "start" for e in events))

    await manager.apply_for_token(
        room.match.id, token, {"type": "move", "move": "H8"}
    )
    await wait_until(lambda: any(e[1].get("type") == "move" for e in events))
    move_event = next(e[1] for e in events if e[1].get("type") == "move")
    assert move_event["seat"] == seat and move_event["move"] == "H8"
    assert move_event["over"] is False

    await manager.apply_for_token(room.match.id, token, {"type": "resign"})
    await wait_until(lambda: any(e[1].get("type") == "resign" for e in events))
    resign_event = next(e[1] for e in events if e[1].get("type") == "resign")
    assert resign_event["over"] is True


# ----------------------------------------------------------------------
#  陪练座位显示名（practice_name）
# ----------------------------------------------------------------------
async def test_practice_name_default_and_configured_reach_bot_seats() -> None:
    """默认「小白」；配置 practice_name 后规则陪练座位用配置名。"""
    manager = make_manager()
    room, _, _ = await create_gomoku(manager, "alice", opponent="bot")
    assert room.match.seats[1].kind == "bot"
    assert room.match.seats[1].name == "小白"

    configured = RoomManager(
        repository=FakeRepo(),
        config=Settings({}),
        now_fn=lambda: 1000,
        bot_name="诺里",
        practice_name="小蓝",
    )
    room2, _, _ = await create_gomoku(
        configured, "bob", mode_options={"mode": "bot", "difficulty": "easy"}
    )
    assert room2.match.seats[1].kind == "bot"
    assert room2.match.seats[1].name == "小蓝"
    # 运行期设置接口同样生效；空值保留默认
    manager.set_practice_name("小蓝")
    assert manager.practice_name == "小蓝"
    manager.set_practice_name("")
    assert manager.practice_name == "小白"


async def test_practice_name_two_bot_seats_numbered() -> None:
    """斗地主双陪练阵容：两个 bot 座位可区分（小蓝 / 小蓝 2）。"""
    manager = make_manager()
    manager.set_practice_name("小蓝")
    room, _, _ = await manager.create(
        kind="doudizhu",
        actor="alice",
        session_id="session-alice",
        name="alice",
        mode_options={"lineup": "bot_bot"},
    )
    bot_names = [s.name for s in room.match.seats if s.kind == "bot"]
    assert bot_names == ["小蓝", "小蓝 2"]


# ----------------------------------------------------------------------
#  真实仓储（host.store.MatchStore）
# ----------------------------------------------------------------------
async def test_room_works_with_match_store(tmp_path: Path) -> None:
    """房间层只依赖 save/get/load_active：真实 SQLite 仓储下恢复/惰性加载一致。"""
    store = MatchStore(tmp_path)
    await store.init()
    try:
        manager = RoomManager(
            repository=store,
            config=Settings({}),
            now_fn=lambda: 1000,
            bot_name="诺里",
        )
        room, seat, token = await create_gomoku(manager, "alice")
        match_id = room.match.id
        assert seat == 0
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
        assert room.dirty is False

        # 惰性加载：只读仓储快照（口令/版本/走子都在）。
        lazy = RoomManager(
            repository=store, config=Settings({}), now_fn=lambda: 1000, bot_name="诺里"
        )
        loaded = await lazy.get_or_load(match_id)
        assert loaded is not None
        assert loaded.match.seats[0].token == token
        assert loaded.match.version == 2
        assert loaded.match.state["moves"] == 1

        # 启动恢复：active 局回到内存，可按口令继续提交。
        restored = RoomManager(
            repository=store, config=Settings({}), now_fn=lambda: 1000, bot_name="诺里"
        )
        await restored.restore()
        assert restored.find_by_actor("alice") is not None
        updated = await restored.apply_for_token(
            match_id, token, {"type": "rename", "name": "恢复后"}
        )
        assert updated.version == 3
        assert updated.seats[0].name == "恢复后"

        # 终局局不再被 load_active 复活。
        await restored.resign_for_actor("alice")
        after = RoomManager(
            repository=store, config=Settings({}), now_fn=lambda: 1000, bot_name="诺里"
        )
        await after.restore()
        assert after.get_room(match_id) is None
        assert after.count() == 0
    finally:
        await store.close()
