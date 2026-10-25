FROM docker.io/library/alpine:3.22
RUN mkdir -p /work && printf 'pear\napple\npear\n' | sort | uniq > /work/fruit && test "$(wc -l < /work/fruit)" -eq 2
RUN sh -c 'exit 7'; code=$?; test "$code" -eq 7 && grep -q apple /work/fruit
CMD ["/bin/cat", "/work/fruit"]
