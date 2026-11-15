#!/bin/sh
# Creates the Secrets the manifests expect, with random values, in the current kubectl context.
# Existing Secrets are left alone: the database keeps the password it was initialized with.
set -eu

NS="${NAMESPACE:-splitsecond}"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

if ! kubectl -n "$NS" get secret postgres-credentials >/dev/null 2>&1; then
    kubectl -n "$NS" create secret generic postgres-credentials \
        --from-literal=POSTGRES_DB=splitsecond \
        --from-literal=POSTGRES_USER=splitsecond \
        --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)"
fi

if ! kubectl -n "$NS" get secret api-secrets >/dev/null 2>&1; then
    kubectl -n "$NS" create secret generic api-secrets \
        --from-literal=JWT_SECRET="$(openssl rand -hex 32)"
fi

# Object storage for backups. Defaults target the in-cluster SeaweedFS on minikube;
# for Spaces set S3_ENDPOINT, S3_REGION, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET and S3_FORCE_PATH_STYLE=false.
if ! kubectl -n "$NS" get secret backup-s3 >/dev/null 2>&1; then
    bucket="${S3_BUCKET:-splitsecond-backups}"
    kubectl -n "$NS" create secret generic backup-s3 \
        --from-literal=AWS_ENDPOINT="${S3_ENDPOINT:-http://seaweedfs:9000}" \
        --from-literal=AWS_REGION="${S3_REGION:-us-east-1}" \
        --from-literal=AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY:-splitsecond}" \
        --from-literal=AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY:-$(openssl rand -hex 24)}" \
        --from-literal=AWS_S3_FORCE_PATH_STYLE="${S3_FORCE_PATH_STYLE:-true}" \
        --from-literal=S3_BUCKET="$bucket" \
        --from-literal=WALG_S3_PREFIX="s3://$bucket/walg"
fi

kubectl -n "$NS" get secrets postgres-credentials api-secrets backup-s3
