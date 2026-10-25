FROM debian:trixie-slim
RUN /bin/bash -c 'set -eu; echo "Debian bash under Blink"; /usr/bin/ldd --version; /usr/bin/id; /usr/bin/uname -m'
CMD ["/usr/bin/apt-get", "--version"]
