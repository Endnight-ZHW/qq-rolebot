FROM mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp MPLCONFIGDIR=/tmp/matplotlib \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY rendering/requirements.txt /opt/requirements.txt
RUN pip install --no-cache-dir -r /opt/requirements.txt
COPY rendering/package*.json /opt/vendor/
RUN cd /opt/vendor && npm ci --ignore-scripts --omit=dev && \
    mkdir -p /opt/www/vendor && cp node_modules/echarts/dist/echarts.min.js /opt/www/vendor/ && \
    cp node_modules/three/build/three.module.js node_modules/three/build/three.core.js /opt/www/vendor/ && rm -rf node_modules
COPY backend/rolebot/creation_contract.py /opt/creation_contract.py
COPY rendering/runner.py rendering/validate.py /opt/
RUN getent passwd 1001 && getent group 1001
COPY rendering/system-packages.json /opt/system-packages.json
# Fail closed if the distribution changes any approved system dependency.
RUN python -c "import json,subprocess; wanted=json.load(open('/opt/system-packages.json')); actual=dict(line.split() for line in subprocess.check_output(['dpkg-query','-W',*wanted],text=True).splitlines()); assert actual==wanted, (actual,wanted)"
USER 1001:1001
WORKDIR /work
CMD ["python","-c","import time; time.sleep(86400)"]
