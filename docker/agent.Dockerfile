# Sandbox image for agent episodes: Python 3.12 + pytest + strace (ledger).
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends strace \
 && rm -rf /var/lib/apt/lists/*
# pytest 9.1.1 and its pure-Python deps, fetched on the host (no pip inside the build).
COPY pydeps/ /usr/local/lib/python3.12/site-packages/
RUN printf '#!/usr/local/bin/python3\nimport sys\nimport pytest\nsys.exit(pytest.main())\n' > /usr/local/bin/pytest \
 && chmod 755 /usr/local/bin/pytest
RUN useradd -m -u 1000 -s /bin/bash agent \
 && mkdir -p /var/ledger /var/opsstate /run/ops /opt/harness /opt/ops \
 && chmod 700 /var/ledger /var/opsstate \
 && chmod 755 /run/ops \
 && mkdir -p /work && chown agent:agent /work
COPY run_cmd.sh fileop.py /opt/harness/
COPY ops_server.py /opt/ops/server.py
COPY opsctl /usr/local/bin/opsctl
RUN chmod 755 /opt/harness/run_cmd.sh /usr/local/bin/opsctl \
 && chmod 644 /opt/harness/fileop.py \
 && chmod 700 /opt/ops
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
