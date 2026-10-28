"""thread_id → agent 映射的持久化测试（见 agent_platform/thread_map.py）。

核心场景：**进程重启后**（新实例、同一 SQLite 文件）仍能查到映射 ——
这是 HITL 挂起过夜、服务重发后 resume() 不退化为"遍历反查"的保障。
"""

from __future__ import annotations

from pathlib import Path

from agent_platform.thread_map import ThreadAgentMap


def test_memory_mode_roundtrip():
    m = ThreadAgentMap(None)
    m.put("th_1", "coaching")
    assert m.get("th_1") == "coaching"
    assert m.get("th_missing") is None


def test_put_overwrites_stale_agent(tmp_path: Path):
    m = ThreadAgentMap(tmp_path / "map.sqlite")
    m.put("th_1", "coaching")
    m.put("th_1", "marketing")  # 同一 thread 重新 invoke 到了另一个 Agent
    assert m.get("th_1") == "marketing"
    m.close()


def test_mapping_survives_process_restart(tmp_path: Path):
    """两个实例先后打开同一文件：模拟服务重启后的恢复路径。"""
    db = tmp_path / "map.sqlite"

    first = ThreadAgentMap(db)
    first.put("th_hitl_001", "flow_ops")
    first.put("th_hitl_002", "voice_service")
    first.close()  # 进程退出

    restarted = ThreadAgentMap(db)  # 新进程：内存为空，必须回源 SQLite
    assert restarted.get("th_hitl_001") == "flow_ops"
    assert restarted.get("th_hitl_002") == "voice_service"
    assert restarted.get("th_unknown") is None
    restarted.close()


def test_concurrent_writes_are_serialized(tmp_path: Path):
    """多线程写不崩、读一致（runtime 在线程池里调用 invoke 的现实场景）。"""
    import threading

    m = ThreadAgentMap(tmp_path / "map.sqlite")
    barrier = threading.Barrier(8)

    def _worker(i: int) -> None:
        barrier.wait()
        m.put(f"th_{i}", f"agent_{i}")
        assert m.get(f"th_{i}") == f"agent_{i}"

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i in range(8):
        assert m.get(f"th_{i}") == f"agent_{i}"
    m.close()
