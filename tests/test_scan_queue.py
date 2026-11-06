import asyncio
import time
from types import SimpleNamespace

import main


def test_async_scans_are_fifo_and_poll_positions_advance(monkeypatch):
    monkeypatch.setattr(main, 'SCAN_CONCURRENCY', 1)
    monkeypatch.setattr(main, 'SCAN_SLOT', asyncio.Semaphore(1))
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

        def eq(self, key, name):
            if key == "analysis_run_id":
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
        assert third.status_message_level == "wait" and third.status_message == main.QUEUE_MESSAGE
        gates["first"].set()
        for _ in range(100):
            if "second" in started:
                break
            await asyncio.sleep(0.01)
        assert rows["first"]["status"] == "failed"
        third = await main.get_scan_status("third", "owner")
        assert third.queue_position == 1 and third.status_message_level == "wait"
        assert third.status_message == main.QUEUE_MESSAGE
        gates["second"].set()
        for _ in range(100):
            if "third" in started:
                break
            await asyncio.sleep(0.01)
        assert (await main.get_scan_status("third", "owner")).queue_position is None
        gates["third"].set()
        await asyncio.gather(*main._scan_workers)
        assert started == ["first", "second", "third"]
        assert len(main._scan_durations) == 3

    asyncio.run(run())


def test_queue_estimate_uses_recent_average(monkeypatch):
    monkeypatch.setattr(main, 'SCAN_CONCURRENCY', 1)
    main._scan_queue.clear()
    main._scan_queue.extend([(None, "second"), (None, "third")])
    main._scan_durations.clear()
    main._scan_durations.extend([60, 180])
    main._running_scan_starts[:] = [time.monotonic() - 30]
    try:
        position, estimate, _, _ = main._queue_state("third")
        assert position == 2 and 208 <= estimate <= 210
    finally:
        main._scan_queue.clear()
        main._scan_durations.clear()
        main._running_scan_starts.clear()


def test_queue_estimate_counts_a_synchronous_scan_holding_the_slot(monkeypatch):
    monkeypatch.setattr(main, 'SCAN_CONCURRENCY', 1)
    monkeypatch.setattr(main, 'SCAN_SLOT', asyncio.Semaphore(1))
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


def test_scans_run_side_by_side_up_to_the_limit(monkeypatch):
    monkeypatch.setattr(main, "SCAN_CONCURRENCY", 2)
    monkeypatch.setattr(main, "SCAN_SLOT", asyncio.Semaphore(2))
    started, gates = [], {name: asyncio.Event() for name in ("a", "b", "c")}

    async def job(_, name, *__, **___):
        started.append(name)
        await gates[name].wait()

    monkeypatch.setattr(main, "_run_scan_job", job)
    main._scan_queue.clear()
    main._scan_workers.clear()

    async def run():
        for name in gates:
            main._enqueue_scan(None, name, None, 0)
        await asyncio.sleep(0.05)
        assert started == ["a", "b"]          # two at once, the third waits
        assert [j[1] for j in main._scan_queue] == ["c"]
        gates["a"].set()
        await asyncio.sleep(0.05)
        assert started == ["a", "b", "c"]
        gates["b"].set(); gates["c"].set()
        await asyncio.gather(*main._scan_workers)

    asyncio.run(run())
