# Pinned by digest; Dependabot (docker ecosystem) bumps the tag and digest together.
FROM node:26-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev
COPY gateway.js ./
USER node
CMD ["node", "gateway.js"]
