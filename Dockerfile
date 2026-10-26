FROM python:3.14-slim

WORKDIR /opt

COPY requirements.txt .

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    python3-dev \
    libffi-dev \
    libjpeg-dev \
    zlib1g-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ /opt/app/
COPY requirements.txt /opt/

WORKDIR /opt

CMD ["python", "-m", "app"]
