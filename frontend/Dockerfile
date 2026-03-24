FROM node:20-alpine

WORKDIR /app

RUN apk add --no-cache curl

COPY package.json package-lock.json* ./
RUN npm install

# Source is bind-mounted in dev (see docker-compose.yml volumes).
# node_modules and .next are excluded via anonymous volumes to keep
# the host copy clean and the container copy intact.

CMD ["npm", "run", "dev"]
