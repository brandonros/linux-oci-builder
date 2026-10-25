# image-forge: network
# Exact package-installation checkpoint from the Claude/code-server stress test.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates python3 dumb-init && rm -rf /var/lib/apt/lists/*
CMD ["/bin/bash", "-c", "git --version && curl --version && python3 --version && dumb-init --version"]
