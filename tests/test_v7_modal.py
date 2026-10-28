import importlib
import subprocess
import sys
from types import SimpleNamespace

from services.ml_client import MLClient, MODAL_APP_NAME


def test_modal_app_constructs_without_importing_torch():
    app = importlib.import_module("modal_app")
    assert app.app.name == MODAL_APP_NAME == "resync-ml-v7"
    assert app.CHECKPOINT.is_file()
    isolated = subprocess.run([sys.executable, "-c",
                           "import modal_app,sys; assert 'torch' not in sys.modules; print(modal_app.app.name)"],
                          capture_output=True, text=True, check=True)
    assert isolated.stdout.strip() == MODAL_APP_NAME


def test_modal_client_calls_only_v7_worker_methods(monkeypatch):
    calls = []

    class Method:
        def __init__(self, name):
            self.name = name

        def remote(self, arg):
            calls.append((self.name, arg))
            return {"ok": True}

    class Worker:
        analyze_v7 = Method("analyze_v7")
        analyze_ai_text = Method("analyze_ai_text")

    class Class:
        @staticmethod
        def from_name(app_name, class_name):
            assert (app_name, class_name) == ("resync-ml-v7", "MLWorker")
            return Worker

    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Cls=Class))
    client = MLClient("modal")
    assert client.analyze_v7([{"name": "A", "text": "a"}]) == {"ok": True}
    assert client.analyze_ai_text({"A": "a"}) == {"ok": True}
    assert calls == [("analyze_v7", [{"name": "A", "text": "a"}]), ("analyze_ai_text", {"A": "a"})]
