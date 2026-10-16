# FalkorDB v4.22.1 amd64 manifest, verified with podman manifest inspect.
FROM docker.io/falkordb/falkordb@sha256:d998ed0dfd91da6208f257dfd61f5330335bd6e7006cfc0fd1ddd761269e2634

USER root
COPY redis.conf /etc/falkordb/redis.conf
COPY entrypoint.sh /usr/local/bin/chronosops-db-entrypoint
RUN chmod 0644 /etc/falkordb/redis.conf && chmod 0755 /usr/local/bin/chronosops-db-entrypoint

# run.sh passes these safely to redis-server and the FalkorDB module at startup.
ENV BROWSER=0 \
    REDIS_ARGS=/etc/falkordb/redis.conf \
    FALKORDB_ARGS="TIMEOUT_MAX 3000 TIMEOUT_DEFAULT 3000"

ENTRYPOINT ["/usr/local/bin/chronosops-db-entrypoint"]
