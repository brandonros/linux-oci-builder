# Separates basic execution from the Bash/coreutils failures.
FROM debian:trixie-slim
RUN /bin/true && /bin/dash -c 'echo Debian-dash-ok'
CMD ["/usr/bin/apt-get", "--version"]
