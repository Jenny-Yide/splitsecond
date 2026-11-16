# Infrastructure runbook

## Local development (Docker Compose)

```sh
cp .env.example .env        # then change the passwords
docker compose up -d --build --wait
curl localhost:8080/health   # repeat: "host" alternates between the two API replicas
```

Compose runs PostgreSQL 16, two API replicas, and nginx on port 8080 (WebSockets on `/ws`).
PostgreSQL is also published on `127.0.0.1:5433` for `psql` or a GUI client.
It also runs the backup pieces described under [Backups](#backups): SeaweedFS (S3 on `127.0.0.1:9100`),
WAL archiving from PostgreSQL, and the `base-backup` service.

Run the ledger tests against the Compose database:

```sh
docker compose --profile test run --rm test
```

`backend/schema.sql` is applied only when the database volume is empty. After changing the schema:

```sh
docker compose down -v && docker compose up -d --build --wait
```

## Kubernetes on minikube

One-time setup:

```sh
brew install minikube
minikube start --driver=docker --cpus=2 --memory=4096
minikube addons enable ingress
./k8s/create-secrets.sh      # random passwords; existing Secrets are kept
```

Deploy (repeat after code changes):

```sh
minikube image build -t splitsecond-api:dev .
kubectl apply -k k8s/overlays/minikube
kubectl -n splitsecond rollout restart deployment/api   # pick up a rebuilt :dev image
```

Reach the app through the ingress:

```sh
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8081:80
curl localhost:8081/health
```

The schema is copied from the API image and applied only when the PostgreSQL volume is new.
To apply a schema change to an existing cluster, either run the SQL by hand
(`kubectl -n splitsecond exec -it postgres-0 -- psql -U splitsecond`) or reset the data:

```sh
kubectl delete -k k8s/overlays/minikube   # keeps the PVC
kubectl -n splitsecond delete pvc data-postgres-0
kubectl apply -k k8s/overlays/minikube
```

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

| Job | Checks |
| --- | --- |
| `ledger-tests` | Applies `backend/schema.sql` to a PostgreSQL 16 service and runs `backend/tests` |
| `compose-smoke` | `docker compose up --build`, then confirms both API replicas answer `/health` through nginx |
| `manifests` | Renders every overlay in `k8s/overlays/` and validates it with kubeconform |

## Health endpoints

| Path | Meaning |
| --- | --- |
| `/livez` | Process is up (Kubernetes liveness probe; does not touch the database) |
| `/health` | Database reachable (readiness probe and uptime check) |
| `/health/backup` | 503 if backups or WAL archiving need attention; see [Backup health](#backup-health) |

## Backups

### Recovery point objective

A nightly dump alone could lose up to 24 hours of expense history. Instead, PostgreSQL continuously
archives its write-ahead log (WAL) to object storage with [wal-g](https://github.com/wal-g/wal-g):

- Every completed WAL segment is uploaded by `archive_command = 'timeout 120 wal-g wal-push %p'`.
  The timeout matters: with storage unreachable, wal-g kept retrying for over 10 minutes without
  failing, which stalls archiving silently. A failed attempt is retried by PostgreSQL and shows up in
  `pg_stat_archiver`.
- `archive_timeout = 300` forces a segment switch at least every 5 minutes when there are writes,
  so at most about **5 minutes** of committed changes are not yet in object storage.
- A daily **base backup** (`wal-g backup-push`) is the starting point for replaying WAL. The last 7 are kept.

Together these allow restoring to any moment covered by the archive (point-in-time recovery).

### Components

| Piece | Where | What it does |
| --- | --- | --- |
| `docker/postgres/Dockerfile` | image `splitsecond-postgres` | `postgres:16` plus pinned, checksum-verified `wal-g` and `s5cmd`; includes `schema.sql` and `backup/` |
| `archive_*` settings | `postgres` container | Push WAL to `$WALG_S3_PREFIX` |
| `backup/base-backup.sh` | `base-backup` sidecar in the `postgres-0` pod (Compose: `base-backup` service) | Every 5 minutes, checks `backup_runs`; if the last successful `base` backup is older than 24 hours, takes one, records it, and applies retention |
| `backup-s3` Secret | `k8s/create-secrets.sh` | Endpoint, region, keys, bucket, and `WALG_S3_PREFIX` |
| SeaweedFS | `k8s/overlays/minikube/object-storage.yaml` (Compose: `seaweedfs`) | Local S3 stand-in for DigitalOcean Spaces |

The sidecar shares the pod because wal-g reads the data directory directly and the volume is ReadWriteOnce.
It runs as the `postgres` user (UID 999) so it can read `PGDATA`.

On DOKS, `backup-s3` points at Spaces instead (see the comment in `create-secrets.sh`); nothing else changes.

### Minikube setup

```sh
minikube image build -t splitsecond-postgres:dev -f docker/postgres/Dockerfile .
./k8s/create-secrets.sh                      # adds backup-s3 if missing
kubectl apply -k k8s/overlays/minikube
```

An existing cluster needs a fresh PostgreSQL volume, because the schema changed
(`backup_runs.kind`): delete the `postgres` StatefulSet and the `data-postgres-0` PVC, then apply.

### Daily logical dump

Independently of wal-g, the `pg-dump` CronJob (`k8s/base/backup-cronjob.yaml`, 03:00 America/Toronto)
runs `backup/dump.sh`. It streams `pg_dump --format=custom` to `s3://$S3_BUCKET/dumps/YYYY-MM-DD/HHMMSS.dump`,
records the run in `backup_runs` (`kind = 'dump'`), and deletes dump folders older than `DUMP_RETAIN_DAYS`
(default 30). A dump restores into any PostgreSQL 16 without wal-g, so it is a second, simpler copy.

```sh
# Kubernetes: run the CronJob now
kubectl -n splitsecond create job --from=cronjob/pg-dump pg-dump-manual-$(date +%s)

# Compose
docker compose --profile backup run --rm dump
```

### Backup health

`/health/backup` returns 503 with a list of `problems` when any of these hold:

| Check | Problem when |
| --- | --- |
| Dump age | No successful `dump` in `backup_runs` within `BACKUP_MAX_AGE_HOURS` (26) |
| Base backup age | No successful `base` within `BACKUP_MAX_AGE_HOURS` (26) |
| WAL archiving failing | `pg_stat_archiver.last_failed_time` is newer than `last_archived_time` |
| WAL archiving behind | Completed WAL segments have waited more than `WAL_ARCHIVE_MAX_LAG_MINUTES` (15) |

The "behind" check exists because a failure alone is not enough: with object storage unreachable,
the first archive failure took about 2 minutes to register even with the `timeout`, and an idle
database archives nothing, so the age of the last archive alone would raise false alarms.
Pointing an uptime check at this endpoint therefore alerts on missed dumps, missed base backups,
and WAL archiving outages.

Tested by stopping SeaweedFS in Compose (with the lag limit set to 1 minute): after 75 seconds the
endpoint reported the segment as behind, after 135 seconds also as failing, and it returned to
200 about 30 seconds after storage came back.

### Useful commands

```sh
# Take a base backup now
kubectl -n splitsecond exec postgres-0 -c base-backup -- /backup/base-backup.sh --once

# List base backups
kubectl -n splitsecond exec postgres-0 -c base-backup -- wal-g backup-list

# WAL archiving status (failed_count should stay 0)
kubectl -n splitsecond exec postgres-0 -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT * FROM pg_stat_archiver"'
```
