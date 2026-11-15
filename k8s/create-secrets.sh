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

kubectl -n "$NS" get secrets postgres-credentials api-secrets
