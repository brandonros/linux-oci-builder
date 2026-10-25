# Network experiment: requires --network in the example runner.
FROM docker.io/library/alpine:3.22
RUN apk add --no-cache curl
CMD ["/usr/bin/curl", "--version"]
