# --- build stage -------------------------------------------------------
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Relative, not a fixed host: the built JS calls same-origin `/api/...`, which
# nginx.conf below reverse-proxies to the backend container. That's what lets
# this one built image work unmodified regardless of which host/domain it's
# eventually served from - no rebuild needed to point at a different backend.
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# --- serve stage ---------------------------------------------------------
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
