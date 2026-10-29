"""One entry point for local or deployed Modal ML batches."""

import os

MODAL_APP_NAME = "resync-ml-v7"
# One scan runs at a time (SCAN_SLOT), so the model can use most cores; set ML_THREADS=1 to go back to the old limit.
ML_THREADS = int(os.getenv("ML_THREADS", min(3, max(1, (os.cpu_count() or 2) - 1))))


class MLClient:
    def __init__(self, backend=None):
        self.backend = backend or os.getenv("ML_BACKEND", "local").strip().lower()
        if self.backend not in ("local", "modal"):
            raise ValueError(f"Unsupported ML_BACKEND: {self.backend}")

    def _call(self, method, *args):
        if self.backend == "local":
            import torch
            torch.set_num_threads(ML_THREADS)
            from services import ml_worker
            return getattr(ml_worker, method)(*args)

        import modal
        try:
            worker = modal.Cls.from_name(MODAL_APP_NAME, "MLWorker")()
            return getattr(worker, method).remote(*args)
        except Exception as exc:
            raise RuntimeError(f"Modal ML {method} failed: {exc}") from exc

    def analyze_v7(self, sections):
        return self._call("analyze_v7", sections)

    def analyze_ai_text(self, sections):
        return self._call("analyze_ai_text", sections)

ml_client = MLClient()
