# Compatibility probe: currently fails because Blink does not provide this file.
FROM docker.io/library/alpine:3.22
RUN cat /proc/self/status
CMD ["/bin/echo", "proc-status-supported"]
