#!/bin/bash
# Non-destructive restore test: rebuilds the database from the wal-g archive in a throwaway pod
# (Service postgres-restore-check:5432). Production is not touched.
#
#   k8s/restore-check.sh latest
#   k8s/restore-check.sh 2026-11-16T01:30:00Z
#   k8s/restore-check.sh --delete
set -euo pipefail

NS="${NAMESPACE:-splitsecond}"
NAME=postgres-restore-check

k() {
    kubectl -n "$NS" "$@"
}

if [[ "${1:-}" == --delete ]]; then
    k delete pod,service -l "app=$NAME" --ignore-not-found
    exit
fi
target="${1:?usage: restore-check.sh <latest|YYYY-MM-DDTHH:MM:SSZ|--delete>}"

image=$(k get statefulset postgres -o jsonpath='{.spec.template.spec.containers[?(@.name=="postgres")].image}')
k delete pod,service -l "app=$NAME" --ignore-not-found --wait >/dev/null
start=$(date +%s)

k apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: $NAME
  labels: {app: $NAME}
spec:
  selector: {app: $NAME}
  ports: [{name: postgres, port: 5432}]
---
apiVersion: v1
kind: Pod
metadata:
  name: $NAME
  labels: {app: $NAME}
spec:
  restartPolicy: Never
  initContainers:
    - name: restore
      image: $image
      command: ["/backup/restore-pitr.sh"]
      envFrom: [{secretRef: {name: backup-s3}}]
      env:
        - {name: PGDATA, value: /var/lib/postgresql/data/pgdata}
        - {name: RESTORE_TARGET, value: "$target"}
      volumeMounts: [{name: data, mountPath: /var/lib/postgresql/data}]
  containers:
    - name: postgres
      image: $image
      # The copy must never archive: it would add a second history to the production archive.
      args: ["-c", "archive_mode=off"]
      envFrom: [{secretRef: {name: postgres-credentials}}, {secretRef: {name: backup-s3}}]
      env: [{name: PGDATA, value: /var/lib/postgresql/data/pgdata}]
      ports: [{containerPort: 5432}]
      volumeMounts: [{name: data, mountPath: /var/lib/postgresql/data}]
  volumes: [{name: data, emptyDir: {}}]
EOF

for _ in $(seq 1 300); do
    # shellcheck disable=SC2016 # expanded inside the container
    state=$(k exec "$NAME" -c postgres -- sh -c \
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT pg_is_in_recovery()"' 2>/dev/null || true)
    if [[ "$state" == f ]]; then
        echo "restored to '$target' and promoted in $(( $(date +%s) - start ))s"
        echo "connect to $NAME:5432 with the production credentials"
        exit 0
    fi
    if [[ "$(k get pod "$NAME" -o jsonpath='{.status.phase}')" == Failed ]]; then
        k logs "$NAME" -c restore --tail=20 || true
        echo "restore failed" >&2
        exit 1
    fi
    sleep 2
done
echo "timed out waiting for recovery" >&2
exit 1
