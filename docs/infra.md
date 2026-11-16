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
| `/health/backup` | 503 if the latest successful row in `backup_runs` is older than 26 hours |
