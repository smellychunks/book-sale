# Sale Lookup: unprivileged nginx with the app and its config baked in. No database, no secrets.
# The library snapshot is NOT in the image: it is mounted at /data (read-only) when the container runs.
FROM nginxinc/nginx-unprivileged:alpine
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/security-headers.inc /etc/nginx/security-headers.inc
COPY app/ /usr/share/nginx/html/
