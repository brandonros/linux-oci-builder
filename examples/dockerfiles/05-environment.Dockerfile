FROM docker.io/library/alpine:3.22
RUN export LOCAL_ONLY=yes; mkdir -p /work && echo persisted > /work/state
RUN test -z "${LOCAL_ONLY:-}" && test "$(cat /work/state)" = persisted && test "$(uname -m)" = x86_64
CMD ["/bin/sh", "-c", "printf 'cwd=%s arch=%s\n' \"$PWD\" \"$(uname -m)\""]
