FROM node:22-bookworm-slim

ENV NODE_ENV=production
ENV TESTPILOT_WORKER_PORT=9444
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV HOME=/tmp

WORKDIR /srv/testpilot-browser-worker
COPY package.json ./
RUN npm install --omit=dev
RUN npx playwright install --with-deps chromium firefox webkit
RUN groupadd --system testpilot
RUN useradd --system --gid testpilot --home-dir /srv/testpilot-browser-worker testpilot
RUN chown -R testpilot:testpilot /srv/testpilot-browser-worker /ms-playwright

COPY --chown=testpilot:testpilot server.mjs browser_sessions.mjs ./
USER testpilot
EXPOSE 9444
CMD ["node", "server.mjs"]