# Stage 1: Build
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

# Stage 2: Serve with nginx (non-root) with HTTPS
FROM nginx:alpine

# Create non-root user for nginx
RUN addgroup -g 101 -S nginx && \
    adduser -S -D -H -u 101 -h /var/cache/nginx -s /sbin/nologin -G nginx -g nginx nginx

COPY --from=build /app/dist /usr/share/nginx/html

# The published HTTPS port: the number a *browser* types, not the number nginx
# listens on inside the container (that stays 8443). docker-compose.yml passes
# 3443 here because 3443 is what it maps 8443 to.
#
# Before this, the redirect was frozen at build as `:8443` — a port the host
# publishes nothing on — so http://localhost:3000 answered
# "go to https://localhost:8443" and the browser reached an empty port.
ARG HTTPS_PORT=8443

# Self-signed pair for local runs (frontend/scripts/generate-certs.sh writes it);
# mount a real certificate over this path for anything that is not a laptop.
# `certs/` is tracked only so this COPY has a target, so copying a directory that
# holds just its own .gitignore is normal: nginx then refuses to start with
# "cannot load certificate", which is the right moment to run that script.
COPY certs/ /etc/nginx/certs/

# Nginx config: serve SPA + proxy API to backend + HTTPS/WSS
#
# The file is written with a token and then substituted, instead of letting the
# shell expand the heredoc: nginx's own variables ($host, $request_uri) live in
# this text, and an unquoted heredoc would eat them on the way into the file.
RUN cat > /tmp/default.conf.in << 'EOF'
# HTTP -> HTTPS redirect
server {
    listen 8080;
    server_name localhost;
    return 301 https://$host:@HTTPS_PORT@$request_uri;
}

# HTTPS server
server {
    listen 8443 ssl http2;
    server_name localhost;

    ssl_certificate /etc/nginx/certs/localhost.crt;
    ssl_certificate_key /etc/nginx/certs/localhost.key;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    root /usr/share/nginx/html;
    index index.html;

    # SPA fallback
    location / {
        try_files $uri $uri/ /index.html;
    }

    # Proxy API to backend
    location /health {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /chat {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /stt {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        client_max_body_size 10m;
    }

    location /command {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /tasks {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /preferences {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /devices {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    # WebSocket proxy (WSS)
    location /ws {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 86400;
    }
}
EOF
# The grep is deliberate: a config that lost its redirect would still start and
# still answer on 8080, i.e. an image that serves plaintext and reports healthy.
RUN sed "s|@HTTPS_PORT@|${HTTPS_PORT}|" /tmp/default.conf.in > /etc/nginx/conf.d/default.conf \
    && rm /tmp/default.conf.in \
    && grep -q "return 301 https://" /etc/nginx/conf.d/default.conf

# Ensure nginx can write to its cache/temp directories
RUN chown -R nginx:nginx /var/cache/nginx /var/run /var/log/nginx /usr/share/nginx/html

# Switch to non-root user
USER nginx

EXPOSE 8080 8443

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    # --no-check-certificate is not a shortcut: the certificate this image ships
    # is the self-signed pair from scripts/generate-certs.sh, so verifying it
    # against a CA that will never exist made every container "unhealthy" even
    # while it was serving. Verification belongs to the browser, which warns.
    CMD wget --no-verbose --tries=1 --spider --no-check-certificate https://localhost:8443/ 2>/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]