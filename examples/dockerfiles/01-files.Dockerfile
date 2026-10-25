FROM docker.io/library/alpine:3.22
RUN mkdir -p /work && printf 'hello\n' > /work/original && cp /work/original /work/copy && mv /work/copy /work/moved && ln /work/original /work/hard && ln -s original /work/link && chmod 640 /work/original
RUN test "$(cat /work/link)" = hello && test /work/hard -ef /work/original && test "$(stat -c %a /work/original)" = 640 && rm /work/moved && test ! -e /work/moved
CMD ["/bin/cat", "/work/original"]
