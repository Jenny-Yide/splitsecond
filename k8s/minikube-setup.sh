#!/bin/sh
# Installs the cluster add-ons SplitSecond needs on minikube. Safe to re-run.
set -eu

ENVOY_GATEWAY_VERSION=v1.10.0
CERT_MANAGER_VERSION=v1.21.3

cd "$(dirname "$0")"

if [ "$(kubectl config current-context)" != "minikube" ]; then
    echo "kubectl context is not minikube; refusing to continue" >&2
    exit 1
fi

# ingress-nginx is retired; routing goes through Gateway API instead.
minikube addons disable ingress >/dev/null 2>&1 || true

# Envoy Gateway; its chart also installs the Gateway API CRDs.
helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm \
    --version "$ENVOY_GATEWAY_VERSION" \
    --namespace envoy-gateway-system --create-namespace --wait

# cert-manager after the Gateway API CRDs, so it can issue certificates for Gateways.
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
    --version "$CERT_MANAGER_VERSION" \
    --namespace cert-manager --create-namespace \
    --set crds.enabled=true \
    --set config.apiVersion=controller.config.cert-manager.io/v1alpha1 \
    --set config.kind=ControllerConfiguration \
    --set config.enableGatewayAPI=true \
    --wait

kubectl apply -f cluster/gatewayclass.yaml
kubectl apply -f cluster/minikube-issuer.yaml
kubectl wait --for=condition=Ready clusterissuer/splitsecond-issuer --timeout=60s

./create-secrets.sh
