"""One entry point for local or deployed Modal ML batches."""

import os


class MLClient:
    def __init__(self, backend=None):
        self.backend = backend or os.getenv("ML_BACKEND", "local").strip().lower()
        if self.backend not in ("local", "modal"):
            raise ValueError(f"Unsupported ML_BACKEND: {self.backend}")

    def _call(self, method, *args):
        if self.backend == "local":
            import torch
            torch.set_num_threads(1)
            from services import ml_worker
            return getattr(ml_worker, method)(*args)

        import modal
        try:
            worker = modal.Cls.from_name("resync-ml", "MLWorker")()
            return getattr(worker, method).remote(*args)
        except Exception as exc:
            raise RuntimeError(f"Modal ML {method} failed: {exc}") from exc

    def analyze_batch(self, parsed_sections, role_texts, role_pairs):
        return self._call("analyze_batch", parsed_sections, role_texts, role_pairs)

    def explain_batch(self, pairs):
        return self._call("explain_batch", pairs)


ml_client = MLClient()
