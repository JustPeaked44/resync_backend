FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cap thread pools before any torch/numpy import — free tier has 0.1 CPU,
# and torch defaults its thread count to the host's core count otherwise.
ENV OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    HF_HOME=/app/.cache/huggingface

# Copy local fine-tuned model directly into the image
COPY models/minilm-dost-v4 /app/models/minilm-dost-v4

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Run the FastAPI application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
