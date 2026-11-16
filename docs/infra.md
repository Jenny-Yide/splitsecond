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

## Restoring

Targets are `latest` (everything archived) or a UTC time in exactly the form `2026-11-16T01:30:00Z`;
recovery stops just before that moment. `backup/restore-pitr.sh` picks the newest base backup that
finished before the target, fetches it, and lets PostgreSQL replay archived WAL up to the target.

### Check a restore without touching production

```sh
k8s/restore-check.sh 2026-11-16T01:30:00Z   # or: latest
```

This starts a throwaway pod and Service, `postgres-restore-check`, on an `emptyDir` with archiving
turned off, so the copy never writes into the production archive. Connect to it with the production
credentials, then remove it with `k8s/restore-check.sh --delete`.

### Recover production

```sh
k8s/restore-check.sh <target>           # first confirm the copy contains what you expect
k8s/recover-postgres.sh <target>        # asks you to type 'restore'
```

`recover-postgres.sh` stores the target in the `postgres-restore` ConfigMap, scales the StatefulSet
to 0, **deletes the `data-postgres-0` PVC**, and scales back up. The StatefulSet's `restore` init
container (`restore-pitr.sh --if-requested`) restores only while that ConfigMap exists and the volume
is empty, so normal restarts are unaffected. After promotion, the script deletes the ConfigMap and
takes a base backup on the new timeline.

A planned recovery, where PostgreSQL is still running, loses nothing past the target: a clean
shutdown archives the current WAL segment.

### Restore a logical dump

```sh
kubectl -n splitsecond exec postgres-0 -c base-backup -- /backup/restore-dump.sh latest restore_check
```

This restores into the named database, creating it if needed, in a single transaction. Pass an
`s3://…/dumps/…` URL instead of `latest` to choose a dump. Restoring into `splitsecond` itself replaces
its contents.

### Verifying balances

`backup/verify_balances.py` (in the API image) computes balances with the ledger's own functions:

```sh
api() { kubectl -n splitsecond exec -i deploy/api -- "$@"; }
api python backup/verify_balances.py snapshot --at 2026-11-16T01:30:00Z > expected.json
# against the restored copy:
api sh -c 'DATABASE_URL=$(echo "$DATABASE_URL" | sed s/@postgres:/@postgres-restore-check:/) \
  python backup/verify_balances.py check -' < expected.json
```

`check` exits 1 if any group's balances differ.

## Restore drills

Run on minikube on 2026-11-16.

| Drill | Result |
| --- | --- |
| Dump: `pg-dump` job, then `restore-dump.sh latest restore_check` | Restored in under 1 s; 2 of 2 groups match the snapshot |
| Point in time: snapshot at T, void every expense, `restore-check.sh T` | Copy restored and promoted in 4 s; 2 of 2 groups match the T snapshot |
| Undo in production: `recover-postgres.sh T` | Voided expenses back; balances match the T snapshot |
| Sudden loss: see below | Lost the last 107 s of writes (53 of 190 expenses); restored balances match; writes resumed 66 s after the crash |

The sudden-loss drill measures the real RPO. `backup/drill_writer.py` creates a group and commits one
expense every 2 seconds, logging each commit's time and the resulting balances:

```sh
api python backup/drill_writer.py --interval 2 --count 190 > writer.jsonl
# crash: freeze every PostgreSQL process so nothing more is archived
kubectl -n splitsecond exec postgres-0 -c postgres -- sh -c \
  'for d in /proc/[0-9]*; do [ "$(cut -d" " -f4 $d/stat)" = 1 ] && kill -STOP ${d#/proc/}; done'
k8s/recover-postgres.sh latest --yes
api python backup/verify_balances.py drill-report - < writer.jsonl
```

`drill-report` finds the last write the restored database contains, reports how many seconds of
writes were lost, and checks that the restored balances equal the balances logged at that write.
Freezing the processes matters: stopping the pod normally is a clean shutdown, which archives
everything and makes the drill lose nothing.

Measured in that drill:

- **RPO: 107 seconds.** The last archived segment was switched at 01:42:46 (`archive_timeout`), and
  the crash came at 01:44:34. Everything committed in between was lost; everything before it was
  restored. The worst case is about 5 minutes, when the crash lands just before the next switch.
- **RTO: 66 seconds** from the crash to accepting writes. Of that, 60 seconds is the pod's
  `terminationGracePeriodSeconds` while the frozen processes ignore shutdown. Fetching the base
  backup and replaying WAL took about 4 seconds on this small database. On a real node failure,
  Kubernetes may also need time to notice the node is gone.

Found by the drills:

- `recovery_target_time` does not accept the ISO `T…Z` form; `restore-pitr.sh` converts it.
- A base backup taken after a restore contains that restore's settings in `postgresql.auto.conf`.
  A later restore from it stopped at the old target, losing everything since, until
  `restore-pitr.sh` started removing earlier recovery settings.
