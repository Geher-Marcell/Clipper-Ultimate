FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
    fonts-liberation \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
    streamlink \
    faster-whisper \
    transformers \
    torch \
    accelerate

WORKDIR /app
COPY clipper.py /app/clipper.py

CMD ["python3", "-u", "/app/clipper.py"]