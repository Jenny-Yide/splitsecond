# Infrastructure runbook

## Local development (Docker Compose)

```sh
cp .env.example .env        # then change the passwords
docker compose up -d --build --wait
curl localhost:8080/health   # repeat: "host" alternates between the two API replicas
```

Compose runs PostgreSQL 16, two API replicas, and nginx on port 8080 (WebSockets on `/ws`).
PostgreSQL is also published on `127.0.0.1:5433` for `psql` or a GUI client.

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
brew install minikube helm
minikube start --driver=docker --cpus=2 --memory=4096
./k8s/minikube-setup.sh      # Envoy Gateway, cert-manager, local CA issuer, Secrets
```

`minikube-setup.sh` is safe to re-run. It installs pinned versions of Envoy Gateway (which brings
the Gateway API CRDs) and cert-manager with Gateway API support, applies the cluster-wide
resources in `k8s/cluster/`, and runs `create-secrets.sh` (random passwords; existing Secrets are kept).

Deploy (repeat after code changes):

```sh
minikube image build -t splitsecond-api:dev .
kubectl apply -k k8s/overlays/minikube
kubectl -n splitsecond rollout restart deployment/api   # pick up a rebuilt :dev image
```

### Routing and HTTPS

Traffic enters through a Gateway API `Gateway` (`k8s/base/gateway.yaml`) served by Envoy Gateway;
ingress-nginx is retired and no longer used. The Gateway has two listeners:

- `http` (80) only redirects to HTTPS (`http-to-https` route).
- `https` (443) terminates TLS and routes everything, including `/ws` WebSockets, to the `api` Service.

cert-manager watches the Gateway (annotation `cert-manager.io/cluster-issuer: splitsecond-issuer`) and
keeps the `splitsecond-tls` certificate issued for the listener hostname. Each cluster defines its own
`splitsecond-issuer`: on minikube it is a local CA (`k8s/cluster/minikube-issuer.yaml`), on DOKS it will
be Let's Encrypt. The minikube overlay sets the hostname to `splitsecond.test`.

Envoy's default timeouts keep WebSockets open: a connection held for 150 seconds received every 30-second
server ping, so no timeout policy is configured.

Reach the app over HTTPS (minikube has no external load balancer, so the Gateway's `Programmed`
condition stays `False` with `AddressNotAssigned`; that is expected):

```sh
SVC=$(kubectl -n envoy-gateway-system get svc \
    -l gateway.envoyproxy.io/owning-gateway-name=splitsecond -o name)
kubectl -n envoy-gateway-system port-forward "$SVC" 8081:80 8443:443

kubectl -n cert-manager get secret splitsecond-local-ca \
    -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/splitsecond-ca.crt
curl --cacert /tmp/splitsecond-ca.crt --resolve splitsecond.test:8443:127.0.0.1 \
    https://splitsecond.test:8443/health
```

Alternatively, run `minikube tunnel` to give the Gateway a real address and add it to `/etc/hosts`
as `splitsecond.test`.

The schema is copied from the API image and applied only when the PostgreSQL volume is new.
To apply a schema change to an existing cluster, either run the SQL by hand
(`kubectl -n splitsecond exec -it postgres-0 -- psql -U splitsecond`) or reset the data:

```sh
kubectl delete -k k8s/overlays/minikube   # keeps the PVC
kubectl -n splitsecond delete pvc data-postgres-0
kubectl apply -k k8s/overlays/minikube
```

## Health endpoints

| Path | Meaning |
| --- | --- |
| `/livez` | Process is up (Kubernetes liveness probe; does not touch the database) |
| `/health` | Database reachable (readiness probe and uptime check) |
| `/health/backup` | 503 if the latest successful row in `backup_runs` is older than 26 hours |
