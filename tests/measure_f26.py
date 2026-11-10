"""Scaled fake Gemini benchmark; no network or manuscript data."""
import asyncio
import collections
import os
import subprocess
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import gemini as current

SCALE = 0.003  # wall seconds per simulated second
WINDOW = 60 * SCALE
DELAY = 2 * SCALE
CALLS = 10


def baseline():
    source = subprocess.check_output(["git", "show", "5c18dcb:services/gemini.py"], text=True, encoding="utf-8")
    module = types.ModuleType("f26_baseline")
    exec(compile(source, "baseline_gemini.py", "exec"), module.__dict__)
    return module


async def measure(module, scan_count, keys, concurrency):
    os.environ["GEMINI_API_KEY"] = "fake-primary"
    if keys == 2:
        os.environ["GEMINI_API_KEY_FALLBACK"] = "fake-secondary"
    else:
        os.environ.pop("GEMINI_API_KEY_FALLBACK", None)
    os.environ["GEMINI_RPM"] = "14"
    module.RATE_WINDOW_SECONDS = WINDOW
    module.RETRY_CAP_SECONDS = 180 * SCALE
    module._quota_blocked_until = {}
    module.LIMITER = module.CallLimiter()
    histories = collections.defaultdict(collections.deque)
    server_lock = asyncio.Lock()

    async def fake_send(_, key):
        async with server_lock:
            history = histories[key]
            while history and time.monotonic() - history[0] >= WINDOW:
                history.popleft()
            if len(history) >= 14:
                await asyncio.sleep(max(0, history[0] + WINDOW - time.monotonic()))
                while history and time.monotonic() - history[0] >= WINDOW:
                    history.popleft()
            assert len(history) < 14
            history.append(time.monotonic())
        await asyncio.sleep(DELAY)
        return types.SimpleNamespace(status_code=200)

    module._send = fake_send
    slots = asyncio.Semaphore(concurrency)
    started = time.monotonic()
    finish = [0.0] * scan_count

    async def scan(index):
        async with slots:
            token = module.scan_context.set(str(index))
            try:
                for _ in range(CALLS):
                    result = await module.post_gemini_with_retry({})
                    assert result.status_code == 200
                finish[index] = (time.monotonic() - started) / SCALE
            finally:
                module.scan_context.reset(token)

    await asyncio.gather(*(scan(i) for i in range(scan_count)))
    return finish


async def main():
    for name, factory, concurrency in (("before", baseline, 1), ("after", lambda: current, 3)):
        for keys in (1, 2):
            for count in (1, 3):
                values = await measure(factory(), count, keys, concurrency)
                print(f"{name} keys={keys} scans={count} finish_s=" + ",".join(f"{v:.2f}" for v in values) + f" total_s={max(values):.2f}")


if __name__ == "__main__":
    asyncio.run(main())
