FROM node:24-alpine AS build
RUN npm install --global pnpm@11.18.0
WORKDIR /app
COPY . .
RUN pnpm install --frozen-lockfile && pnpm build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
EXPOSE 80 443
