import asyncio
import json
import threading
import time
import pytest
from fastapi import HTTPException

import main
from services import credits


def test_two_concurrent_unlocks_charge_once(monkeypatch):
    state = {"owner": "user", "balance": 1, "unlocked": False, "charges": 0}
    lock = threading.Lock()

    class Request:
        def __init__(self, args):
            self.args = args

        def execute(self):
            with lock:
                time.sleep(0.01)
                if self.args["p_user_id"] != state["owner"]:
                    raise RuntimeError("SCAN_NOT_FOUND")
                if state["unlocked"]:
                    value = {"unlocked": True, "charged": False, "credits_remaining": state["balance"]}
                elif self.args["p_charge"] and state["balance"] == 0:
                    raise RuntimeError("INSUFFICIENT_CREDITS")
                else:
                    state["charges"] += int(self.args["p_charge"])
                    state["balance"] -= int(self.args["p_charge"])
                    state["unlocked"] = True
                    value = {"unlocked": True, "charged": bool(self.args["p_charge"]),
                             "credits_remaining": state["balance"]}
                return type("Response", (), {"data": value})()

    class DB:
        def rpc(self, name, args):
            assert name == "unlock_scan_report" and args["p_analysis_run_id"] == "scan"
            return Request(args)

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    monkeypatch.setattr(credits, "CREDITS_ENABLED", True)

    async def pair():
        return await asyncio.gather(main.unlock_scan("scan", "user"), main.unlock_scan("scan", "user"))

    responses = asyncio.run(pair())
    assert sorted(response["charged"] for response in responses) == [False, True]
    assert [response["credits_remaining"] for response in responses] == [0, 0]
    assert state == {"owner": "user", "balance": 0, "unlocked": True, "charges": 1}
    assert asyncio.run(main.unlock_scan("scan", "user")) == {
        "unlocked": True, "charged": False, "credits_remaining": 0}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.unlock_scan("scan", "other"))
    assert exc.value.status_code == 404

    state["unlocked"] = False
    denied = asyncio.run(main.unlock_scan("scan", "user"))
    assert denied.status_code == 402
    assert json.loads(denied.body) == {
        "detail": "Not enough credits to unlock this report.", "credits_remaining": 0}
    assert state["charges"] == 1


def test_unlock_kill_switch_does_not_charge(monkeypatch):
    called = {}

    class Request:
        def execute(self):
            return type("Response", (), {"data": {"unlocked": True, "charged": False,
                                                   "credits_remaining": 0}})()

    class DB:
        def rpc(self, name, args):
            called.update(args)
            return Request()

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    monkeypatch.setattr(credits, "CREDITS_ENABLED", False)
    assert asyncio.run(main.unlock_scan("scan", "user")) == {
        "unlocked": True, "charged": False, "credits_remaining": 0}
    assert called == {"p_user_id": "user", "p_analysis_run_id": "scan", "p_charge": False}
