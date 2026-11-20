#!/bin/sh
# Installs the cluster add-ons SplitSecond needs on DOKS. Safe to re-run.
#
# DOKS already ships Gateway API CRDs and a Cilium GatewayClass, so Envoy Gateway
# is not installed here (its chart conflicts with DOKS-managed CRDs). Minikube
# still uses Envoy via minikube-setup.sh.
#
# Required environment:
#   LETSENCRYPT_EMAIL   contact for Let's Encrypt expiry notices
#   S3_ENDPOINT         e.g. https://tor1.digitaloceanspaces.com
#   S3_REGION           e.g. tor1
#   S3_ACCESS_KEY       Spaces key
#   S3_SECRET_KEY       Spaces secret
# Optional:
#   S3_BUCKET           default splitsecond-backups
set -eu

CERT_MANAGER_VERSION=v1.21.3

cd "$(dirname "$0")"

context=$(kubectl config current-context)
case "$context" in
    minikube|docker-desktop)
        echo "kubectl context is '$context'; refusing to run DOKS setup" >&2
        exit 1
        ;;
esac

: "${LETSENCRYPT_EMAIL:?set LETSENCRYPT_EMAIL to a real address}"
: "${S3_ENDPOINT:?set S3_ENDPOINT (e.g. https://tor1.digitaloceanspaces.com)}"
: "${S3_REGION:?set S3_REGION (e.g. tor1)}"
: "${S3_ACCESS_KEY:?set S3_ACCESS_KEY}"
: "${S3_SECRET_KEY:?set S3_SECRET_KEY}"

if ! kubectl get gatewayclass cilium >/dev/null 2>&1; then
    echo "GatewayClass 'cilium' not found; this DOKS cluster needs Gateway API enabled" >&2
    exit 1
fi

# cert-manager with Gateway API support (issues certificates for the Gateway).
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
    --version "$CERT_MANAGER_VERSION" \
    --namespace cert-manager --create-namespace \
    --set crds.enabled=true \
    --set config.apiVersion=controller.config.cert-manager.io/v1alpha1 \
    --set config.kind=ControllerConfiguration \
    --set config.enableGatewayAPI=true \
    --wait

sed "s/LETSENCRYPT_EMAIL_PLACEHOLDER/$LETSENCRYPT_EMAIL/" \
    cluster/letsencrypt-issuer.yaml | kubectl apply -f -
kubectl wait --for=condition=Ready clusterissuer/splitsecond-issuer --timeout=120s

S3_BUCKET="${S3_BUCKET:-splitsecond-backups}" \
S3_FORCE_PATH_STYLE=false \
    ./create-secrets.sh
