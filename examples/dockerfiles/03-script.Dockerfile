FROM docker.io/library/alpine:3.22
RUN mkdir -p /app && printf '#!/bin/sh\nset -eu\nname=${1:-world}\nprintf "Hello, %%s!\\n" "$name"\n' > /app/greet && chmod +x /app/greet
RUN test "$(/app/greet Blink)" = 'Hello, Blink!'
CMD ["/app/greet", "image-forge"]
