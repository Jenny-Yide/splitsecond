#!/bin/bash
# Disaster recovery: replaces the production database volume with a point-in-time restore
# from the wal-g archive. This DELETES the current volume (data-postgres-0); run
# k8s/restore-check.sh with the same target first to confirm it contains what you expect.
#
#   k8s/recover-postgres.sh latest
#   k8s/recover-postgres.sh 2026-11-16T01:30:00Z
#   k8s/recover-postgres.sh latest --yes     # no confirmation prompt
set -euo pipefail

NS="${NAMESPACE:-splitsecond}"
target="${1:?usage: recover-postgres.sh <latest|YYYY-MM-DDTHH:MM:SSZ> [--yes]}"

k() {
    kubectl -n "$NS" "$@"
}

if [[ "${2:-}" != --yes ]]; then
    read -r -p "Delete PVC data-postgres-0 in '$NS' and restore to '$target'? Type 'restore': " answer
    [[ "$answer" == restore ]] || exit 1
fi

start=$(date +%s)
# The postgres StatefulSet's restore init container acts only while this ConfigMap exists.
k create configmap postgres-restore --from-literal=target="$target" --dry-run=client -o yaml | k apply -f -
k scale statefulset postgres --replicas=0
k wait --for=delete pod/postgres-0 --timeout=180s || true
k delete pvc data-postgres-0 --ignore-not-found --wait
k scale statefulset postgres --replicas=1

for _ in $(seq 1 300); do
    # shellcheck disable=SC2016 # expanded inside the container
    state=$(k exec postgres-0 -c postgres -- sh -c \
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT pg_is_in_recovery()"' 2>/dev/null || true)
    [[ "$state" == f ]] && break
    sleep 2
done
if [[ "$state" != f ]]; then
    k logs postgres-0 -c restore --tail=20 || true
    echo "recovery did not finish; the postgres-restore ConfigMap is still in place" >&2
    exit 1
fi
k delete configmap postgres-restore
echo "database restored to '$target' and accepting writes after $(( $(date +%s) - start ))s"

# Recovery promoted a new timeline; start a fresh base backup chain on it.
k exec postgres-0 -c base-backup -- /backup/base-backup.sh --once
