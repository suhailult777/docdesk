# Sandbox for running a doc's commands. Built once with network access;
# every run then uses `docker run --network none`.
FROM node:20-alpine
RUN apk add --no-cache curl
USER node
WORKDIR /home/node
