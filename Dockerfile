ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONHASHSEED=0 \
    CUDA_VISIBLE_DEVICES=-1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libmagic1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-lock.txt requirements-torch.txt ./

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && python -m pip install --upgrade pip \
 && python -m pip install -r requirements-torch.txt --index-url https://download.pytorch.org/whl/cpu \
 && if python -m pip install -r requirements-lock.txt; then \
        echo "Installed the pinned lock file (requirements-lock.txt)"; \
    else \
        echo "WARNING: requirements-lock.txt did not install on Linux; installing requirements.txt instead" \
        && python -m pip install -r requirements.txt; \
    fi \
 && python -c "import torch, transformers, garak, yaml, pandas, matplotlib; print('imports ok, torch', torch.__version__)" \
 && apt-get purge -y --auto-remove build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY . .
RUN mkdir -p results logs data/splits

CMD ["python", "-m", "scripts.run_pipeline"]