# AI Interaction Record (`ai-session.md`)

This document records four representative AI interactions from building and deploying **SplitSecond**, providing concrete evidence for the **AI Assistance & Verification** section in [`README.md`](README.md).

Across the project, our team framed the core ledger guarantees (integer cents, append-only versioning, row-level locking, and non-lossy persistence) ourselves first, and used AI assistants (Cursor / LLM chat) to critique architectural trade-offs, scaffold Docker/Kubernetes/backup automation, and troubleshoot cloud and UI issues. Early proposal-stage explorations (such as rejecting an AI suggestion for CPU-based Horizontal Pod Autoscaling in favor of continuous WAL archiving and point-in-time recovery, and verifying PostgreSQL `LISTEN/NOTIFY` commit/no-replay semantics against the official PostgreSQL documentation) are documented in [`proposal-README.md`](proposal-README.md). Below are four verbatim development and deployment sessions illustrating how we evaluated, corrected, and technically verified AI output.

---

## Session 1: Re-architecting Ingress and Backup RPO After Proposal Feedback

### Prompt

```text
1. I have merged the changes
2. here is the feedback and see if this changes anything: SplitSecond presents a clear expense-sharing problem, with thoughtful choices for rounding, concurrent edits, and persistent history. The proposal distinguishes database records from live notifications and explains why backup and recovery are more useful for the expected workload than autoscaling. The examples of evaluating AI suggestions and checking PostgreSQL documentation also show meaningful independent reasoning.

A related scenario worth considering is one person changing an item claim while another finalizes the bill, or two people finalizing the same bill simultaneously. These cases could affect which claims are included or create duplicate expenses. Consider how the proposed transaction and version checks could ensure that finalization produces one expense from a consistent set of claims.

The scheduled backups and restore plan are useful, but a nightly backup can still leave up to a day's changes unrecoverable. Relating the backup frequency to an acceptable loss of expense history would help clarify that tradeoff. The named ingress-nginx controller has also been retired, so the deployment should use a maintained ingress or Gateway controller.

3. plan on the work that does not require DigitalOcean
```

### AI Response

*(Trimmed from our planning and follow-up PR breakdown response)*

```text
Finalization already locks the bill row (`SELECT ... FOR UPDATE`), so simultaneous finalizations can't double-charge; the open concern is a race between claim changes and finalize, since inserting or deleting `item_claims` without locking the bill under READ COMMITTED could persist a claim right as finalize reads the bill. The claim endpoints should lock the bill row (`FOR SHARE`) so claims and `FOR UPDATE` finalization serialize cleanly.

The feedback changes two infrastructure decisions: replacing retired `ingress-nginx` and tightening backup RPO from 24 hours to ~5 minutes. I've split the non-DigitalOcean work into stacked PRs, each built on `main` and tested locally:

1. PR 1, CI: runs the ledger tests against PostgreSQL 16, a 2-replica Docker Compose smoke test, and `kubeconform` Kubernetes manifest validation on every PR and push.
2. PR 2, `backup_runs.kind`: adds a `kind` column (`'dump'` | `'base'`) so `backup_runs` tracks both logical dumps and physical base backups.
3. PR 3, Gateway API and HTTPS: replaces `ingress.yaml` (`ingress-nginx`) with Kubernetes Gateway API (`Envoy Gateway` on minikube) and `cert-manager` TLS with HTTP-to-HTTPS redirect.
4. PR 4, WAL archiving: builds a PostgreSQL 16 image with `wal-g` and `s5cmd`, archives WAL segments continuously (`archive_timeout = 300`) to S3-compatible storage, and runs a `base-backup` sidecar sharing the RWO PVC.
5. PR 5, daily dump and health checks: adds the `pg_dump` CronJob and extends `/health/backup` to report stale dumps, stale base backups, and failing WAL archiving.
6. PR 6, restore tooling and drills: adds non-destructive `restore-check.sh`, production `recover-postgres.sh`, logical `restore-dump.sh`, and balance verification against ledger snapshots.
```

### What Your Team Did With It

- **What was useful:** Breaking the rework into small, reviewable PRs starting with GitHub Actions CI (`d1d17ab`) caught regressions early; separating `/livez` (process liveness) from `/health` (DB readiness) prevented database restarts from causing API pod crash loops; and passing the `FOR SHARE` (claim) vs. `FOR UPDATE` (finalize) lock design to our application/ledger teammates ensured concurrent claims and finalization could never produce inconsistent shares (`backend/app/api.py`, `backend/tests/test_finalize_concurrency.py`).
- **What was incomplete or buggy in the initial AI-generated backup and restore scripts:** First, our initial `archive_command = 'wal-g wal-push %p'` had no process timeout—when we stopped local S3 (`SeaweedFS`) to simulate an object-storage outage, `wal-g` retried internally and hung for over 10 minutes without exiting non-zero, stalling PostgreSQL WAL archiving silently and delaying `pg_stat_archiver` failure reporting. Second, relying on `last_archived_time` age alone would trigger false-positive 503 alerts whenever the database was idle (no writes = no new WAL segments). Third, running actual point-in-time recovery (PITR) drills exposed two bugs in the AI-drafted `backup/restore-pitr.sh`: PostgreSQL rejected ISO-8601 `2026-11-16T01:11:19Z` strings in `recovery_target_time`, and base backups taken after a recovery preserved old recovery targets inside `postgresql.auto.conf`, causing later restores to halt too early.
- **How our team verified, modified, and corrected it:** We wrapped WAL uploads in `timeout 120 wal-g wal-push %p` (`504086f`) and added a pending-WAL `.ready` segment lag check (`WAL_ARCHIVE_MAX_LAG_MINUTES=15`) in `/health/backup` (`d9e6993`), verifying by stopping `SeaweedFS` that `/health/backup` returned HTTP 503 after 75 s (lag) and 135 s (archive failure) and recovered to 200 within 30 s of storage returning. We fixed `restore-pitr.sh` (`990cb93`) to normalize timestamps and strip prior recovery directives from `postgresql.auto.conf`, then ran four restore drills on minikube using `backup/drill_writer.py` (1 write / 2 s, freezing PostgreSQL with `SIGSTOP` to prevent a clean-shutdown WAL flush) and `backup/verify_balances.py`, measuring a real **107 s RPO** and **66 s RTO** with 100% balance agreement.

---

## Session 2: Deploying Gateway API on DigitalOcean Kubernetes (Envoy Gateway CRD Conflict vs. Cilium)

### Prompt

```text
check the new code, and give detailed instruction for DigitalOcean implementation
```

*(Followed by scaffolding `k8s/overlays/doks/` and `k8s/doks-setup.sh`, running `./k8s/doks-setup.sh` against our new DOKS cluster, and sharing the terminal Helm failure output.)*

### AI Response

*(Trimmed from the initial DOKS deployment plan and scaffolded setup instructions before testing on the cluster)*

```text
## 3. One-time cluster setup

Same Helm pins as minikube (`k8s/minikube-setup.sh`), minus the minikube-only pieces:

# Envoy Gateway + Gateway API CRDs
helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm \
  --version v1.10.0 -n envoy-gateway-system --create-namespace --wait

# cert-manager with Gateway API
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
  --version v1.21.3 -n cert-manager --create-namespace \
  --set crds.enabled=true \
  --set config.apiVersion=controller.config.cert-manager.io/v1alpha1 \
  --set config.kind=ControllerConfiguration \
  --set config.enableGatewayAPI=true --wait

kubectl apply -f k8s/cluster/gatewayclass.yaml
kubectl apply -f k8s/cluster/letsencrypt-issuer.yaml

Give the cluster pull access to the registry (`doctl registry kubernetes-config`).
...
kubectl -n envoy-gateway-system get svc \
  -l gateway.envoyproxy.io/owning-gateway-name=splitsecond
```

### What Your Team Did With It

- **What was useful:** Confirming that the merged FastAPI + static SPA + WebSocket codebase (`6894516`) could be served from a single container image behind one HTTPS/WSS Gateway hostname, keeping a clean Kustomize split (`k8s/base` shared, `do-block-storage` PVC and Let's Encrypt `ClusterIssuer` in `k8s/overlays/doks`), and catching in a follow-up check that DOKS's UI defaulted node-pool autoscaling to `min=0 / max=1` (which we disabled so our stateful PostgreSQL node could never scale to zero) and that a 2 GiB node would OOM once kubelet, Postgres, sidecar, `cert-manager`, and 2 API replicas were scheduled (so we chose 1× 4 GiB / 2 vCPU).
- **What was incorrect in the AI response:** Two cloud-specific commands and assumptions failed immediately against a real DOKS cluster:
  1. **Gateway API CRD conflict:** AI assumed we should install Envoy Gateway via Helm on DOKS just like on minikube. In reality, **DOKS pre-installs and manages the Gateway API CRDs itself and ships a built-in `cilium` `GatewayClass`**; Envoy Gateway's Helm chart attempted to overwrite DOKS-managed CRDs and was rejected by the cluster.
  2. **Outdated `doctl` CLI syntax:** AI suggested `doctl registry kubernetes-config` (and earlier `doctl kubernetes create --name splitsecond`), neither of which matched current `doctl` commands (`doctl kubernetes cluster registry add splitsecond` and `doctl kubernetes cluster create splitsecond`).
- **How our team verified, modified, and replaced it:** We inspected `kubectl get crd | grep gateway` and `kubectl get gatewayclass` on DOKS, uninstalled the failed `eg` Helm release, and redesigned `k8s/doks-setup.sh` and `k8s/overlays/doks/kustomization.yaml` (`e6944d8`) to skip Envoy on DOKS entirely—keeping Envoy Gateway only for local minikube while patching the DOKS `Gateway` to use `gatewayClassName: cilium` with `service.beta.kubernetes.io/do-loadbalancer-tls-passthrough: "true"` so Let's Encrypt TLS terminates inside the cluster. We validated the overlay with `kubectl kustomize` and `shellcheck`, pointed `splitsecond.duckdns.org` at the Cilium LoadBalancer IP, and verified `Certificate` `READY=True` plus HTTP 200 on `https://splitsecond.duckdns.org/health` and `/health/backup`.

---

## Session 3: Debugging Broken Container Image Pushes to DOCR (`zsh` Expansion, `arm64`/`amd64`, BuildKit Attestations, and Registry Tier Misdiagnosis)

### Prompt

```text
what's next
```
*(After running the AI's suggested `docker build -t $REGISTRY:api .` and `docker push`, pods failed to pull `:api` and `:postgres` showed up as 5 KB in DOCR. We followed up:)*
```text
I think we need to change DNS of docker or something
```
*(And after the AI next blamed our DigitalOcean Container Registry subscription tier, we checked the DigitalOcean console screenshot and replied:)*
```text
already 5G
```

### AI Response

*(Sequence of three AI responses across the debugging exchange, trimmed)*

```text
# Initial response (incomplete/broken on macOS + zsh):
REGISTRY=registry.digitalocean.com/splitsecond/splitsecond
docker build -t $REGISTRY:api .
docker build -t $REGISTRY:postgres -f docker/postgres/Dockerfile .
docker push $REGISTRY:api
docker push $REGISTRY:postgres

# Second response (after :api failed to build/push):
Not DNS. zsh ate the tag.
In zsh, `$REGISTRY:api` is treated as a path modifier (`:a` = absolute path), so it became
`/Users/.../splitsecond/registry.digitalocean.com/.../splitsecond` + `pi`.
Fix — always quote the tag: `"${REGISTRY}:api"`.

# Third response (after tags in DOCR still appeared as ~5 KB manifests):
Found it: Starter registry is too small, and the pushes didn't land real layers.
- Starter limit: ~500 MB
- Your images: API ~284 MB + Postgres ~788 MB -> over the limit
1. Upgrade the registry to Basic ($5/mo, 5 GB):
   doctl registry update --subscription-tier-slug basic
2. Rebuild for Linux amd64 and push cleanly (DOKS nodes are amd64; a Mac may have built arm64):
   export BUILDX_NO_DEFAULT_ATTESTATIONS=1
   docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
     -t "${REGISTRY}:api" --push .
```

### What Your Team Did With It

- **What was useful:** Catching that our `invalid reference format` failure had nothing to do with Docker DNS—`zsh` interprets `:a` in unbraced `$REGISTRY:api` as an absolute-path history/parameter modifier—and identifying both the Apple Silicon (`arm64`) vs. DOKS (`amd64`) architecture mismatch and Docker BuildKit's default provenance/SBOM attestation manifests that caused DOCR tags to appear as 5 KB index stubs.
- **What was incorrect or incomplete:**
  1. The AI's initial deploy commands omitted parameter braces, `--platform linux/amd64`, and `--provenance=false --sbom=false`, which polluted DOCR with unusable manifests and caused the DOKS node to cache a broken/wrong-architecture image.
  2. When pushes still showed ~5 KB tags, the AI **confidently misdiagnosed the root cause as hitting the 500 MB Starter registry quota** and told us to run a paid tier upgrade—even though checking the DigitalOcean Control Panel proved our registry was **already on the 5 GiB Basic plan** (with only ~179 MiB used).
  3. Because `:api` and `:postgres` are non-`:latest` tags, Kubernetes defaulted `imagePullPolicy` to `IfNotPresent`, so re-pushing the fixed `amd64` image to the same tag still left pods failing on the node's cached stale image until we forced pulls.
- **How our team verified, modified, and corrected it:** We rejected the unnecessary registry tier change, rebuilt and pushed both images with `BUILDX_NO_DEFAULT_ATTESTATIONS=1` and `docker buildx build --platform linux/amd64 --provenance=false --sbom=false -t "${REGISTRY}:api" --push .`, ran `doctl registry garbage-collection start --include-untagged-manifests --force`, and verified real compressed image sizes (`61 MB` for `api`, `191 MB` for `postgres`) via `doctl registry repository list-tags splitsecond`. We also added `imagePullPolicy: Always` to `k8s/overlays/doks/kustomization.yaml` and documented every build/push gotcha in `docs/infra.md` (`e6944d8`).

---

## Session 4: Diagnosing a Misleading `"reconnecting"` WebSocket Indicator in the Web UI

### Prompt

*(Attached screenshot of the Groups list screen showing a grey `reconnecting` badge in the top navigation bar)*

```text
what does the reconnecting mean
```
*(Followed by:)*
```text
should we fix it
```

### AI Response

```text
"reconnecting" means the browser's WebSocket to the API is down, and the client is trying to open it again.

On the groups list page that badge is misleading: the app only connects a WebSocket when you're inside a group (`connectGroup`), and on the groups list it disconnects and sets the badge to `hidden`.

Yes. It's a real bug, and the fix is small. Two things cause it:

1. CSS: `.live { display: flex }` overrides the browser's `[hidden]`, so the badge can still show when the HTML says `hidden`.
2. JS race: on intentional disconnect (leaving a group -> groups list), `ws.onclose` still calls `setLive(false)` and flips the badge back to "reconnecting".
```

### What Your Team Did With It

- **What was useful:** Tracing the visual glitch across both CSS and asynchronous WebSocket lifecycle code instead of papering over only one symptom. Adding `[hidden] { display: none !important; }` fixed the login screen, while guarding `ws.onclose` with `if (state.ws !== ws || state.wsGroup !== gid) return;` before `setLive(false)` (`backend/app/static/app.js`) stopped intentional navigation out of a group from flashing `"reconnecting"`.
- **What required correction during the same UI modernization pass:** In the same UI polish branch (`ui/modern-polish`, merged in PR #13 `198f64b`), two issues surfaced when we tested the AI's stylesheet changes in a live mobile viewport (`390×680`): first, the sticky disabled `Finalize` CTA button inherited `button:disabled { opacity: .45 }`, making it translucent so text from the `So far` bill summary card bled through underneath; second, rebuilding only the `api` service in Docker Compose caused `nginx` to return HTTP `502 Bad Gateway` because `nginx` cached the old API container IPs.
- **How our team verified, modified, and corrected it:** We gave `.bill-cta button:disabled` an opaque background in `backend/app/static/style.css`, validated JS syntax with `node --check backend/app/static/app.js`, restarted `nginx` alongside `api` in Compose, and verified end-to-end across login, groups list, expense creation, history timeline, and two-session live-bill WebSocket item claiming on both mobile (`390px`) and desktop (`1024px`) viewports before redeploying to DOKS.
