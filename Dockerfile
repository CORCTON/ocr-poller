FROM node:22-bookworm-slim

# ocr requires git >= 2.41; bookworm (and bookworm-backports) only ship 2.39,
# so build a recent git from source.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gcc make curl ca-certificates \
      zlib1g-dev libssl-dev libcurl4-gnutls-dev libexpat1-dev \
 && curl -fsSL https://www.kernel.org/pub/software/scm/git/git-2.51.0.tar.gz \
      | tar -xz -C /tmp \
 && cd /tmp/git-2.51.0 \
 && make -j"$(nproc)" prefix=/usr NO_GETTEXT=1 NO_TCLTK=1 NO_PERL=1 NO_PYTHON=1 install \
 && cd / && rm -rf /tmp/git-2.51.0 \
 && apt-get purge -y gcc make zlib1g-dev libssl-dev libcurl4-gnutls-dev libexpat1-dev \
 && apt-get autoremove -y \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @alibaba-group/open-code-review \
 && git --version \
 && ocr version

WORKDIR /app
COPY poll.py run_poster.js ./
COPY vendor ./vendor
RUN mkdir -p /state /work

CMD ["python3", "/app/poll.py"]
