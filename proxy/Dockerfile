FROM docker.io/library/nginx:1.27-alpine
COPY nginx.conf /etc/nginx/nginx.conf
# Default demo credentials: acme:acme123 and globex:globex123
# Generated with htpasswd -bc
RUN echo 'acme:$apr1$Vq8iP0.h$q.z0NksX8j608rZ3G4N3n/' > /etc/nginx/.htpasswd \
    && echo 'globex:$apr1$bT7oY9/x$3zT5G0lB3R2Z3p7M9x1Vv/' >> /etc/nginx/.htpasswd \
    && chmod 644 /etc/nginx/.htpasswd
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
