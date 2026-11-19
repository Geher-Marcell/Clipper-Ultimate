FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    build-essential \
    ffmpeg \
    fonts-liberation \
    libgl1 \
    libglib2.0-0 \
    libatomic1 \
    libnss3 \
    libasound2 \
    libgbm1 \
    libxshmfence1 \
    wget \
    ca-certificates \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends ./google-chrome-stable_current_amd64.deb \
    && rm -f google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# Upgrade Ubuntu 22.04's default pip (22.0.2) so modern wheels resolve cleanly
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel

# Install core clipping, transcription, and upload libraries
RUN pip3 install --no-cache-dir --upgrade \
    "numpy<2" \
    streamlink \
    faster-whisper \
    instagrapi \
    "moviepy<2.0" \
    tiktok-uploader \
    google-api-python-client \
    google-auth-oauthlib

# Download Playwright Chromium (all OS shared libraries are already installed above)
RUN python3 -m playwright install chromium

# Install headless OpenCV cleanly
RUN rm -rf /usr/local/lib/python3*/dist-packages/cv2* \
    && pip3 install --no-cache-dir "numpy<2" opencv-python-headless \
    && python3 -c "import cv2; assert hasattr(cv2, 'CascadeClassifier'); print('OpenCV verified:', cv2.__version__)"

WORKDIR /app
COPY clipper.py uploader.py /app/

CMD ["python3", "-u", "/app/clipper.py"]