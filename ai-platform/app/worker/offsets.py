"""Kafka 位点水位线（``docs/08`` §5.4「offset 提交」的落地手段）。

**不能「谁先跑完谁先提交」**：同一个分区里 offset 5、6 两条消息并发执行，若 6 先完成就先提交
到 7，此时进程崩溃 —— offset 7 之前还没处理的 5 永远不会再被投递，**任务静默丢失**。Kafka 的
offset 是「分区内单调的下一个消费位置」，它表达的是「这之前我都处理完了」。

所以只能提交**连续水位线**：分区内 offset 集合形成 ``{…, 8}`` 这样的连续前缀，前缀末尾才允许
提交。这就是这个类存在的全部理由。

它被单独拿出来还有一个原因：这段逻辑是纯函数式、与 Kafka 无关的，只用整数就能把它测透。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, order=True)
class Position:
    """一条消息的位置（主题 + 分区 + offset）。"""

    topic: str
    partition: int
    offset: int


@dataclass(slots=True)
class _PartitionState:
    """单个分区的位点状态。"""

    #: 已提交（或可安全提交）的最后一个 offset；``None`` = 「还没见过任何消息」。
    #: **不能**用 ``-1`` 兼作哨兵：``-1`` 恰好也是「已对齐到 offset 0 之前」的合法水位线
    #: （即刚 track 过 offset 0）。两者混在一起会让 ``complete`` 把「乱序完成的 offset 2」
    #: 当成新起点，把水位线直接推到 2 —— 正是这个类要防的丢消息。
    watermark: int | None = None
    #: 已完成但还没并进水位线的 offset（乱序完成的那些）
    done: set[int] = field(default_factory=set)
    #: 在飞（已取到、还没完成）的 offset
    inflight: set[int] = field(default_factory=set)


class OffsetTracker:
    """按分区维护「已完成 offset 的连续前缀」，产出可安全提交的位点。"""

    def __init__(self) -> None:
        self._states: dict[tuple[str, int], _PartitionState] = {}

    # ------------------------------------------------------------------
    def track(self, position: Position) -> None:
        """登记一条**在飞**消息。

        首次见到某分区时把水位线对齐到 ``offset - 1``：否则第一条消息完成时会算出
        「前缀没连上」而永不提交（表现为「处理完了但重启后重复消费」）。
        """
        state = self._state(position)
        if state.watermark is None:
            state.watermark = position.offset - 1
        state.inflight.add(position.offset)

    def complete(self, position: Position) -> dict[tuple[str, int], int]:
        """标记完成并推进水位线。

        Returns:
            ``{(topic, partition): 下一个待消费的 offset}``；**没有推进时返回空字典**
            （此时调用方不该白跑一次提交）。
        """
        state = self._state(position)
        if state.watermark is None:
            # 没 track 过就 complete：说明调用方漏了登记。按「这条是新起点」处理，比抛异常好
            # —— 抛异常会让消费循环因一条消息崩掉。
            state.watermark = position.offset - 1
        state.inflight.discard(position.offset)
        state.done.add(position.offset)
        if state.watermark == position.offset - 1:
            # 只有紧邻水位线的那一条（或由它接续的一串）才能推进
            while state.watermark + 1 in state.done:
                state.watermark += 1
                state.done.discard(state.watermark)
            return {(position.topic, position.partition): state.watermark + 1}
        return {}

    def commit_positions(self) -> dict[tuple[str, int], int]:
        """当前所有分区可安全提交的位点（收尾时用）。"""
        return {
            key: state.watermark + 1
            for key, state in self._states.items()
            if state.watermark is not None
        }

    def inflight(self, key: tuple[str, int] | None = None) -> int:
        """在飞条数（不传 ``key`` 时为全局）。"""
        if key is not None:
            state = self._states.get(key)
            return len(state.inflight) if state is not None else 0
        return sum(len(state.inflight) for state in self._states.values())

    def seen(self, key: tuple[str, int]) -> bool:
        """是否见过该分区。"""
        return key in self._states

    def reset(self) -> None:
        """清空（测试用）。"""
        self._states.clear()

    # ------------------------------------------------------------------
    def _state(self, position: Position) -> _PartitionState:
        key = (position.topic, position.partition)
        state = self._states.get(key)
        if state is None:
            state = _PartitionState()
            self._states[key] = state
        return state


__all__ = ["OffsetTracker", "Position"]
