"""Deploy with `modal deploy modal_app.py` after configuring a Modal token."""

import os

import modal

app = modal.App("resync-ml")
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("numpy~=2.5.1", "spacy~=3.8.14")
    .pip_install_from_requirements("requirements-ml.txt")
    .workdir("/app")
    .add_local_dir("services", "/app/services")
    .add_local_dir("models/model_v5b", "/app/models/model_v5b")
    .add_local_file("models/v6_model.pt", "/app/models/v6_model.pt")
)
# Optional so deploys keep working until the v7 checkpoint is added to the repo.
if os.path.exists("models/v7_final_model.pt"):
    image = image.add_local_file("models/v7_final_model.pt", "/app/models/v7_final_model.pt")


def analyze_batch(parsed_sections, role_texts, role_pairs):
    from services.ml_worker import analyze_batch as run
    return run(parsed_sections, role_texts, role_pairs)


def analyze_v7(sections):
    from services.ml_worker import analyze_v7 as run
    return run(sections)


def explain_batch(pairs):
    from services.ml_worker import explain_batch as run
    return run(pairs)


@app.cls(image=image, cpu=1.0, memory=2048, timeout=600, min_containers=0)
class MLWorker:
    @modal.enter()
    def load_models(self):
        import torch
        torch.set_num_threads(1)
        from services import embedding, xai_utils  # noqa: F401 - load once per container

    @modal.method()
    def analyze_batch(self, parsed_sections, role_texts, role_pairs):
        return analyze_batch(parsed_sections, role_texts, role_pairs)

    @modal.method()
    def analyze_v7(self, sections):
        return analyze_v7(sections)

    @modal.method()
    def explain_batch(self, pairs):
        return explain_batch(pairs)
