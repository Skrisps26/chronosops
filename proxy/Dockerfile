FROM docker.io/library/nginx@sha256:62223d644fa234c3a1cc785ee14242ec47a77364226f1c811d2f669f96dc2ac8

# The official Nginx entrypoint renders /etc/nginx/templates/*.template before
# launching Nginx. Keep credentials outside the image and mount them at runtime.
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY 05-load-proxy-token.sh /docker-entrypoint.d/05-load-proxy-token.envsh
RUN rm -f /etc/nginx/conf.d/default.conf \
    && chmod 755 /docker-entrypoint.d/05-load-proxy-token.envsh
ENV NGINX_ENVSUBST_FILTER=^CHRONOSOPS_PROXY_TOKEN$

EXPOSE 8080
