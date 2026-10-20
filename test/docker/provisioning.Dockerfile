FROM node:22-trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv curl ca-certificates && rm -rf /var/lib/apt/lists/*
# A real aplexer binary is a test prerequisite, never host state/config/accounts.
COPY aplexer /usr/local/bin/aplexer
RUN useradd --create-home --uid 10001 hubtest
COPY lifecycle.py /opt/lifecycle.py
USER hubtest
ENV HOME=/home/hubtest PATH=/home/hubtest/.local/bin:/usr/local/bin:/usr/bin:/bin
WORKDIR /home/hubtest
CMD ["python3", "/opt/lifecycle.py"]
