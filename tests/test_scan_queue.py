import asyncio
import time
from types import SimpleNamespace

import main


def test_async_scans_are_fifo_and_poll_positions_advance(monkeypatch):
    rows = {name: {"analysis_run_id": name, "user_id": "owner", "status": "processing"}
            for name in ("first", "second", "third")}
    started = []
    gates = {name: asyncio.Event() for name in rows}

    class DB:
        def table(self, *_):
            return self

        def select(self, *_):
            return self

        def update(self, value):
            self.value = value
            return self

        def eq(self, _, name):
            self.name = name
            return self

        def execute(self):
            if hasattr(self, "value"):
                rows[self.name].update(self.value)
                del self.value
            return SimpleNamespace(data=[rows[self.name]])

    async def pipeline(_, name, *__ , **kwargs):
        started.append(name)
        await gates[name].wait()
        if name == "first":
            raise RuntimeError("test failure")
        return SimpleNamespace(model_dump=lambda **_: {"analysis_run_id": name})

    async def save(result):
        rows[result.model_dump()["analysis_run_id"]]["status"] = "completed"
        rows[result.model_dump()["analysis_run_id"]]["result_json"] = result.model_dump()

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    monkeypatch.setattr(main, "_execute_scan_pipeline", pipeline)
    monkeypatch.setattr(main, "_save_completed_result", save)
    main._scan_queue.clear()
    main._scan_durations.clear()

    async def run():
        for name in rows:
            main._enqueue_scan(None, name, None, 0)
        await asyncio.sleep(0)
        assert started == ["first"]
        third = await main.get_scan_status("third", "owner")
        assert third.queue_position == 2 and third.estimated_wait_seconds >= 240
        assert third.status_message_level == "wait" and "#2" in third.status_message
        gates["first"].set()
        for _ in range(100):
            if "second" in started:
                break
            await asyncio.sleep(0.01)
        assert rows["first"]["status"] == "failed"
        third = await main.get_scan_status("third", "owner")
        assert third.queue_position == 1 and third.status_message_level == "info"
        assert "You're next in line" in third.status_message
        gates["second"].set()
        for _ in range(100):
            if "third" in started:
                break
            await asyncio.sleep(0.01)
        assert (await main.get_scan_status("third", "owner")).queue_position is None
        gates["third"].set()
        await main._scan_worker
        assert started == ["first", "second", "third"]
        assert len(main._scan_durations) == 3

    asyncio.run(run())


def test_queue_estimate_uses_recent_average(monkeypatch):
    main._scan_queue.clear()
    main._scan_queue.extend([(None, "second"), (None, "third")])
    main._scan_durations.clear()
    main._scan_durations.extend([60, 180])
    main._running_scan_started = time.monotonic() - 30
    try:
        position, estimate, _, _ = main._queue_state("third")
        assert position == 2 and 208 <= estimate <= 210
    finally:
        main._scan_queue.clear()
        main._scan_durations.clear()
        main._running_scan_started = None


def test_queue_estimate_counts_a_synchronous_scan_holding_the_slot():
    main._scan_queue.clear()
    main._scan_queue.append((None, "waiting"))
    main._scan_durations.clear()

    async def run():
        async with main.SCAN_SLOT:
            assert main._queue_state("waiting")[:2] == (1, 240)

    try:
        asyncio.run(run())
    finally:
        main._scan_queue.clear()
