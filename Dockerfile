FROM node:22-bookworm-slim

# ocr requires git >= 2.41; bookworm ships 2.39, so pull git from backports.
RUN echo "deb http://deb.debian.org/debian bookworm-backports main" \
      > /etc/apt/sources.list.d/backports.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends -t bookworm-backports git \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @alibaba-group/open-code-review \
 && git --version \
 && ocr version

WORKDIR /app
COPY poll.py run_poster.js ./
COPY vendor ./vendor
RUN mkdir -p /state /work

CMD ["python3", "/app/poll.py"]
