# image-forge: network
# Diagnostic variant: use --dns-tcp with blink-suite.
# A larger initial cache avoids APT's first mremap call; it does not fix mremap.
FROM debian:trixie-slim
RUN apt-get update && apt-get -o APT::Cache-Start=134217728 install -y --no-install-recommends git curl ca-certificates python3 dumb-init && rm -rf /var/lib/apt/lists/*
CMD ["/bin/bash", "-c", "git --version && curl --version && python3 --version && dumb-init --version"]
