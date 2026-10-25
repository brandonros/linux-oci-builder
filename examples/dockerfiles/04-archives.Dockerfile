FROM docker.io/library/alpine:3.22
RUN mkdir -p /work/source /work/restored && printf 'archive-test\n' > /work/source/message && tar -czf /work/data.tar.gz -C /work/source .
RUN tar -xzf /work/data.tar.gz -C /work/restored && cmp /work/source/message /work/restored/message && sha256sum /work/restored/message > /work/checksum
CMD ["/bin/cat", "/work/checksum"]
