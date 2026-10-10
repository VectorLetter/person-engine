# Multi-architecture index: neither build nor runtime is fixed to arm64.
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PERSON_ENGINE_HOME=/data/person-engine PERSON_ENGINE_PHOTO_ROOT=/media/photos \
    PERSON_ENGINE_MODEL_ROOT=/models PERSON_ENGINE_PROVIDER=real \
    PERSON_ENGINE_HOST=0.0.0.0 PERSON_ENGINE_PORT=8000
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/person-engine
COPY requirements-lock.txt requirements-api-lock.txt ./
# BuildKit cache is outside image layers. Delete dependency example models in the
# same layer as installation: they are not runtime inputs and are not distributed.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-compile -r requirements-api-lock.txt \
    && rm -rf /usr/local/lib/python3.12/site-packages/onnxruntime/datasets \
              /usr/local/lib/python3.12/site-packages/cv2/data \
    && find /usr/local/lib/python3.12/site-packages -type d -name tests -prune -exec rm -rf '{}' +
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md SECURITY.md ./
COPY src ./src
# Runtime/reference docs only; historical gate/validation reports stay outside the image.
COPY docs/docker-runtime.md docs/local-api.md docs/openapi.json \
     docs/public-v0-api-contract.md docs/public-v0-error-codes.md \
     docs/public-v0-limitations.md docs/snapshot-contract.md \
     docs/synthetic-acceptance.md docs/linux-decode-compatibility.md \
     docs/licensing.md docs/calibration.md docs/privacy.md docs/usage.md \
     docs/architecture.md docs/dependency-licenses.json docs/api-dependency-licenses.json ./docs/
RUN pip install --no-cache-dir --no-compile --no-build-isolation --no-deps . \
    && pip check \
    && rm -rf build src/*.egg-info \
    && useradd --uid 10001 --create-home --no-log-init engine \
    && mkdir -p /data/person-engine /media/photos /models \
    && chown 10001:10001 /data/person-engine
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).close()"
ENTRYPOINT ["python", "-m", "person_engine.container_runtime"]
