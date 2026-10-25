FROM debian:trixie-slim
RUN useradd --uid 1000 --create-home --shell /bin/bash coder
CMD ["/bin/bash", "-c", "id coder && test -d /home/coder && getent passwd coder"]
