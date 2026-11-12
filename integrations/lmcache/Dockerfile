FROM vllm/vllm-openai-cpu:v0.31.0-arm64@sha256:045fb648a61cfcd32d4b9ca5d0c391013afadae0178d2e23ac292597f2f64036

ARG LMCACHE_REVISION=d0959e006063dcd8addd1b441d8ee5dd755ef366
ENV NO_GPU_EXT=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0 \
    LMCACHE_MP_TRANSFER_MODE=engine_driven

RUN python3 -c "import importlib.metadata as m; print('torch==' + m.version('torch'))" > /tmp/torch-constraint.txt \
    && curl -fsSL "https://codeload.github.com/LMCache/LMCache/tar.gz/${LMCACHE_REVISION}" | tar -xz -C /opt \
    && mv "/opt/LMCache-${LMCACHE_REVISION}" /opt/lmcache \
    && python3 -m pip install --no-cache-dir -c /tmp/torch-constraint.txt -r /opt/lmcache/requirements/build.txt -r /opt/lmcache/requirements/common.txt -r /opt/lmcache/requirements/cli.txt \
    && python3 -m pip install --no-cache-dir --no-deps --no-build-isolation /opt/lmcache \
    && python3 -m pip uninstall -y opentelemetry-exporter-otlp-common \
    && python3 -m pip check \
    && python3 -m pip freeze > /opt/lmcache-installed.txt \
    && python3 /opt/lmcache/lmcache/v1/multiprocess/transport/grpc_impl/_proto_gen/_generate.py \
    && python3 -c "import vllm; from lmcache.integration.vllm.lmcache_mp_connector import LMCacheMPConnector; assert vllm.__version__ == '0.31.0', vllm.__version__"

LABEL org.opencontainers.image.lmcache.revision=${LMCACHE_REVISION}
