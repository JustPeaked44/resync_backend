"""Deploy the isolated v7 ML worker with `modal deploy modal_app.py`."""

from pathlib import Path
import modal
from services.ml_client import MODAL_APP_NAME

CHECKPOINT = Path(__file__).parent / "models" / "v7_final_model.pt"
if modal.is_local() and not CHECKPOINT.is_file():
    raise FileNotFoundError(f"Required v7 checkpoint missing: {CHECKPOINT}")

app = modal.App(MODAL_APP_NAME)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("numpy~=2.5.1", "spacy~=3.8.14")
    .pip_install_from_requirements("requirements-ml.txt")
    .workdir("/app")
    .add_local_dir("services", "/app/services")
    .add_local_dir("models/model_v5b", "/app/models/model_v5b")
    .add_local_file(str(CHECKPOINT), "/app/models/v7_final_model.pt")
)


@app.cls(image=image, cpu=1.0, memory=2048, timeout=600, min_containers=0)
class MLWorker:
    @modal.enter()
    def load_models(self):
        import torch
        torch.set_num_threads(1)
        from services.v7_logic import get_model
        get_model()

    @modal.method()
    def analyze_v7(self, sections):
        from services.ml_worker import analyze_v7
        return analyze_v7(sections)

    @modal.method()
    def analyze_ai_text(self, sections):
        from services.ml_worker import analyze_ai_text
        return analyze_ai_text(sections)
