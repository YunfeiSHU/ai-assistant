"""分批提交位点的连续水位线（``docs/08`` §5.4 的实现核心）。

这一段单独成文件是有原因的：并发消费下「谁先跑完谁提交 offset」是**静默丢消息**
的经典写法 —— 提交了 5 之后 offset 3 还失败着，重启就再也看不到它，而日志里
一句错都没有。所以要覆盖的不是「能提交」，而是：

* 乱序完成时**只能**提交连续前缀（跳号就停）；
* 首条消息之前没有历史位点时不误判（``offset-1`` 对齐）；
* 没推进时**不**产出空提交（否则每次空转都往 broker 发一次无意义的请求）。
"""

from __future__ import annotations

from app.worker.offsets import OffsetTracker, Position


def _pos(offset: int, *, topic: str = "ai.task.document.ingest", partition: int = 0) -> Position:
    return Position(topic=topic, partition=partition, offset=offset)


def test_empty_tracker_returns_nothing() -> None:
    """没登记过任何消息时不该产出位点（不能凭空提交 ``0``）。"""
    assert OffsetTracker().commit_positions() == {}


def test_single_message_advances_watermark() -> None:
    """第一条消息完成 → 提交 ``offset + 1``（Kafka 提交的是「下一条」）。"""
    tracker = OffsetTracker()
    tracker.track(_pos(7))
    assert tracker.complete(_pos(7)) == {("ai.task.document.ingest", 0): 8}


def test_out_of_order_completion_waits_for_gap() -> None:
    """跳号完成只记下、不推进；缺口补上后一次性推进到底。"""
    tracker = OffsetTracker()
    for offset in (0, 1, 2):
        tracker.track(_pos(offset))

    assert tracker.complete(_pos(2)) == {}  # 2 先完成，但 0、1 还在飞
    assert tracker.complete(_pos(1)) == {}
    # 0 完成时，0/1/2 已成连续前缀 → 一次提交到 3
    assert tracker.complete(_pos(0)) == {("ai.task.document.ingest", 0): 3}
    assert tracker.inflight() == 0


def test_first_seen_offset_is_not_treated_as_gap() -> None:
    """从 offset 10 开始消费（新分区/位点被重置）时也要立刻能提交。"""
    tracker = OffsetTracker()
    tracker.track(_pos(10))
    assert tracker.complete(_pos(10)) == {("ai.task.document.ingest", 0): 11}


def test_complete_without_track_is_tolerated() -> None:
    """漏了 ``track`` 也按「这就是新起点」处理：一条消息不该把消费循环打崩。"""
    tracker = OffsetTracker()
    assert tracker.complete(_pos(3)) == {("ai.task.document.ingest", 0): 4}


def test_partitions_advance_independently() -> None:
    """不同分区各自维护水位线，互不阻塞。"""
    tracker = OffsetTracker()
    tracker.track(_pos(0, partition=0))
    tracker.track(_pos(0, partition=1))
    assert tracker.complete(_pos(0, partition=1)) == {("ai.task.document.ingest", 1): 1}
    assert tracker.complete(_pos(0, partition=0)) == {("ai.task.document.ingest", 0): 1}


def test_topics_are_tracked_separately() -> None:
    """同一分区号在不同主题上是两条独立的流。"""
    tracker = OffsetTracker()
    tracker.track(_pos(0, topic="ai.task.summary_build"))
    tracker.track(_pos(0, topic="ai.task.memory_extract"))
    assert tracker.complete(_pos(0, topic="ai.task.summary_build")) == {
        ("ai.task.summary_build", 0): 1
    }
    assert tracker.seen(("ai.task.memory_extract", 0)) is True


def test_commit_positions_reports_every_seen_partition() -> None:
    """收尾提交要覆盖**所有**见过的分区（漏一个分区 = 那一批消息重放）。"""
    tracker = OffsetTracker()
    tracker.track(_pos(0))
    tracker.track(_pos(0, topic="ai.task.summary_build"))
    tracker.complete(_pos(0))
    tracker.complete(_pos(0, topic="ai.task.summary_build"))
    assert tracker.commit_positions() == {
        ("ai.task.document.ingest", 0): 1,
        ("ai.task.summary_build", 0): 1,
    }


def test_reset_clears_state() -> None:
    """``reset`` 之后当作全新消费者（重连场景）。"""
    tracker = OffsetTracker()
    tracker.track(_pos(0))
    tracker.reset()
    assert tracker.seen(("ai.task.document.ingest", 0)) is False
    assert tracker.inflight() == 0
