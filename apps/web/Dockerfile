# syntax=docker/dockerfile:1

# ---------- Stage 1: install dependencies (cached until the lockfile changes) ----------
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci = install exactly what package-lock.json says; fail if it's out of sync.
RUN npm ci

# ---------- Stage 2: build ----------
FROM node:24-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---------- Stage 3: slim runtime ----------
FROM node:24-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Only the standalone server and its static assets; no full node_modules, no source.
# (No public/ folder: it has no tracked files, so it doesn't exist in a clean clone.
#  Add `COPY --from=builder /app/public ./public` if static files are ever added there.)
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

# The official node image ships an unprivileged "node" user.
USER node
EXPOSE 3000

# Healthcheck: node:alpine has no curl, but busybox wget is built in;
# docker-compose.yml uses `wget` to probe the page.
CMD ["node", "server.js"]
