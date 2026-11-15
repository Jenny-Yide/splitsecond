# PostgreSQL with wal-g (WAL archiving, base backups, point-in-time restore) and s5cmd (S3 uploads).
# Build from the repo root: docker build -f docker/postgres/Dockerfile .
FROM postgres:16

ARG WALG_VERSION=v3.0.9
ARG S5CMD_VERSION=2.3.0
ARG TARGETARCH

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) walg_arch=amd64; s5cmd_arch=64bit ;; \
        arm64) walg_arch=aarch64; s5cmd_arch=arm64 ;; \
        *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    walg="wal-g-pg-22.04-${walg_arch}"; \
    curl -fsSLO "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/${walg}.tar.gz"; \
    curl -fsSLO "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/${walg}.tar.gz.sha256"; \
    sha256sum -c "${walg}.tar.gz.sha256"; \
    tar -xzf "${walg}.tar.gz"; \
    install -m 0755 "${walg}" /usr/local/bin/wal-g; \
    s5cmd="s5cmd_${S5CMD_VERSION}_Linux-${s5cmd_arch}.tar.gz"; \
    curl -fsSLO "https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/${s5cmd}"; \
    curl -fsSLO "https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/s5cmd_checksums.txt"; \
    grep " ${s5cmd}\$" s5cmd_checksums.txt | sha256sum -c -; \
    tar -xzf "${s5cmd}" s5cmd; \
    install -m 0755 s5cmd /usr/local/bin/s5cmd; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/* /tmp/*; \
    wal-g --version; \
    s5cmd version

COPY backend/schema.sql /docker-entrypoint-initdb.d/10-schema.sql
COPY backup/ /backup/
