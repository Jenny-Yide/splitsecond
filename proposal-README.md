[SplitSecond-proposal.md](https://github.com/user-attachments/files/34343726/SplitSecond-proposal.md)
# SplitSecond: ECE1779 Project Proposal

| Team member | Student number | Email |
| --- | --- | --- |
| Jenny Qin | 1006960068 | yide.qin@mail.utoronto.ca |
| Cheng Zhao | 1012816954 | chan.zhao@mail.utoronto.ca |
| Fuchun Chai | 1003052082 | fuchun.chai@mail.utoronto.ca |

## 1. Motivation

Students constantly split rent, groceries, trips, and dinners. Two things tend to go wrong.

First, equal splits are often unfair. If one person orders a $14 noodle bowl and another a $38 steak and two drinks, an even split is quick but unfair. Splitting fairly means working out items, tax, and tip by hand.

Second, shared records are fragile. Groups track debts in chats or spreadsheets, where entries get overwritten or deleted and nobody remembers why a balance changed. Splitwise helps, but itemized splits still mean one person assigns every item, and a wrong edit is hard to undo.

SplitSecond is a shared expense ledger for roommates, travel groups, and friends, adding two things existing tools lack. With **live bill claiming**, the payer enters the receipt once and everyone at the table claims their own items from their phone at the same time. With a **ledger history**, every change is kept, so users can see balances as of any past date and restore an expense that was edited or deleted by mistake.

This fits the course well: a money ledger cannot lose or corrupt data, so persistent storage and backups are core requirements, not extras.

## 2. Objectives, Architecture, and Key Features

### 2.1 Core features

Users can:

- Sign up, create a group, and invite members.
- Add an expense split equally, by exact amounts, or by shares.
- Create a live bill from receipt items. Members claim items in real time; shared items are split among claimants, and tax and tip in proportion to each subtotal.
- See balances and a simplified settle-up plan (at most n − 1 payments for n people), and record payments.
- Browse the full history, view balances on any past date, and restore an edited or deleted expense.

### 2.2 Architecture

The backend is a stateless Python FastAPI service with a simple web UI that works on phones. It runs as two replicas on DigitalOcean Kubernetes (DOKS), behind a Gateway API `Gateway` (ingress-nginx is retired). On DOKS the Gateway uses DigitalOcean's built-in Cilium GatewayClass; locally on minikube we use Envoy Gateway instead, because DOKS manages the Gateway API CRDs and does not allow Envoy to replace them. All state is in PostgreSQL, which runs as a single-replica StatefulSet with its data on a DigitalOcean Volume. PostgreSQL continuously archives its write-ahead log (WAL) to DigitalOcean Spaces, alongside a daily base backup and a daily logical dump, so a failure loses at most about five minutes of changes. The API is stateless, so any replica serves any request and we need no sticky sessions, even for WebSockets (2.5).

The main trade-off is that the database is a single point of failure. DigitalOcean Volumes are ReadWriteOnce, so if the database's node fails, the volume must be reattached elsewhere and the app is down for a few minutes. Replicated PostgreSQL would avoid this but adds failover logic we could not test in time. For an expense app, brief downtime is acceptable but losing data is not, so we spend our effort on backups and tested restores.

### 2.3 Data model and correctness

The main tables are `users`, `groups`, `group_members`, `expenses`, `expense_shares`, `bills`, `bill_items`, `item_claims`, `settlements`, and `backup_runs`. Key design choices:

- **Integer cents with a fixed rounding rule.** Leftover cents go one each to members ordered by user ID, so $100 split three ways is always 3334, 3333, and 3333 cents. Tax and tip use the largest-remainder method, so every part still adds up exactly. A transaction is rejected if shares do not add up to the total.
- **Append-only ledger.** Each row belongs to a logical expense (`expense_id`) and has a `version`. Editing voids the current row and inserts version + 1; deleting voids it; restoring inserts a copy of an old version. Balances are calculated from non-voided rows, so they cannot drift, and past balances are a query on `created_at` and `voided_at`.
- **Optimistic concurrency.** An edit carries the version the user loaded. If someone else saved first, it is rejected with HTTP 409 instead of silently overwriting their change.
- **Idempotency.** New expenses carry a client-generated key with a `UNIQUE` constraint, so a retry or double tap returns the existing expense instead of creating a duplicate.
- **Order-independent claims.** Each claim is a row with `UNIQUE(item_id, user_id)`. Finalizing a bill happens in one transaction that checks every item is claimed and turns the claims into a normal expense.
- **Access control.** Every query is filtered by group membership.

### 2.4 Required technologies

| Requirement | Plan |
| --- | --- |
| Docker and Docker Compose | One API image. Locally, Compose runs two API replicas, PostgreSQL, and nginx, so we test multiple replicas from the start. |
| PostgreSQL | Stores the ledger; its constraints and transactions enforce correctness. |
| Persistent storage | PostgreSQL StatefulSet with a PVC backed by a DigitalOcean Volume, so data survives pod restarts and rescheduling. |
| Deployment | DigitalOcean: DOKS, Container Registry, Volumes, and Spaces. |
| Orchestration | Kubernetes: minikube locally, then DOKS. A Deployment and Service for the API, a StatefulSet and PVC for the database with a base-backup sidecar, a dump CronJob, and a Gateway with HTTPRoutes. We chose Kubernetes over Swarm mainly for StatefulSets and CronJobs. |
| Monitoring | DigitalOcean alerts on node CPU, memory, and disk, and a DigitalOcean Uptime check on `/health`. Each dump and base backup is recorded in `backup_runs`. `/health/backup` returns 503 if either is over 26 hours old, or if WAL archiving is failing or over 15 minutes behind, so a second Uptime check turns a backup problem into an alert. |

We are not planning auto-scaling; two replicas for availability is enough for this traffic (see 4.2).

### 2.5 Advanced features

1. **Real-time updates.** Live bills use WebSockets. A browser stays connected to one replica, but its claims may be handled by another. Each replica holds a dedicated PostgreSQL connection that `LISTEN`s on a channel and forwards events to its clients, avoiding Redis. `NOTIFY` is delivered only after commit, so clients never see a rolled-back claim. Payloads are capped at 8000 bytes, so we send only the bill ID and event type. Missed messages are not stored, so clients reload the full bill on reconnect. Proxies and load balancers can drop idle connections, so the server sends a ping every 30 seconds.
2. **Backup and recovery.** The in-app history covers user mistakes. For infrastructure failures, a nightly backup alone could lose a full day of expenses, so we size backups to the loss we accept: about five minutes. PostgreSQL archives its WAL to Spaces with wal-g, forcing a new segment at least every five minutes (`archive_timeout`), and a sidecar takes a daily base backup. Together they allow point-in-time recovery to any moment in the last seven days, for example to just before a bad change. A daily `pg_dump` CronJob adds a simpler second copy that restores without wal-g. Credentials come from a Kubernetes Secret. Restores are tested with drills: a script restores into a separate database and checks that every group's balances match a snapshot taken with the ledger's own functions, and a simulated crash during writes measures the actual data loss and recovery time.
3. **Security.** Hashed passwords, JWT sessions, group-level access checks, HTTPS on the Gateway with cert-manager and Let's Encrypt (HTTP redirects to HTTPS), and credentials stored in Kubernetes Secrets.

GitHub Actions runs the ledger tests and checks the Compose stack and Kubernetes manifests on every pull request; automatic deployment is a stretch goal.

### 2.6 Scope

Out of scope: multiple currencies, real payments, receipt scanning, and native apps. The scope suits three people because live bills and history share the same ledger code, and the hardest piece (point-in-time recovery) is optional.

## 3. Project Plan and Team Responsibilities

| Dates | Work | Deadlines |
| --- | --- | --- |
| Nov 4 – 10 | Ledger core with tests, agreed API models, Compose setup | Proposal (Nov 7) |
| Nov 11 – 17 | Live bills, history view, minikube. Milestone: end-to-end slice on minikube (create a bill, claim from two phones, finalize, see balances) | Team health check (Nov 16) |
| Nov 18 – 24 | DOKS deployment, backups, monitoring, demo rehearsal | Introduction (Nov 23), slides (Nov 24) |
| Nov 25 – Dec 5 | Presentation, restore testing, stretch goals, video, report | Presentation (Nov 25), final (Dec 5) |

| Member | Responsibilities |
| --- | --- |
| Cheng Zhao | Ledger and data: schema, split and rounding logic, versioning, settle-up, history queries, bill finalization |
| Jenny Qin | Application: API, authentication and permissions, web UI, WebSockets and `LISTEN/NOTIFY` |
| Fuchun Chai | Infrastructure: Docker, Compose, Kubernetes, storage, HTTPS, backups, monitoring |

**Integration.** In week one, Cheng and Jenny will agree on the API models and ledger function signatures, so the API can be built against stubs while the ledger is finished, and Fuchun will set up Compose so we all share one environment. Live bills depend on both the claims schema and the WebSocket layer, so Cheng and Jenny will build that feature together in week two. Pull requests are reviewed by someone from another area, so everyone understands the whole system, and each week we deploy main and rehearse the demo.

**Risks.** Concurrency or rounding bugs (covered by tests in week one); DigitalOcean cost (we develop on minikube and run DOKS only from week three); and point-in-time recovery taking too long (so it is optional).

## 4. Initial Team Reasoning and AI-Assisted Exploration

### 4.1 Initial Team Reasoning

We used AI only to brainstorm topics (4.2). After choosing expense splitting, we worked out the problem and a first design as a team before asking AI for design help.

**Problem framing.** After a group dinner, one of us spent twenty minutes working out who owed what from a receipt photo, and the group chat still argued about the tip. We felt a useful tool had to split costs by what each person actually ordered, and keep a record that everyone in the group trusts.

**Initial technical hypotheses.** We planned to store each member's running balance and update it with every expense. We also assumed one API container and one PostgreSQL container would be enough, and that restarting a container would keep its data.

**Open questions.** How do we keep the database safe if its pod is deleted? If two people edit the same expense at once, whose change wins? How can several API replicas push live updates to the same group? How should rounding work when $100 is split three ways?

### 4.2 Exploration and Evaluation

**Choosing the topic.** The AI's brainstorm led us to look first at an event check-in system. We switched to expense splitting because the check-in idea felt like something we would only build for the course. We all split rent and dinners weekly, so we understand the users and can test on ourselves.

**Choosing features.** Asked for more original directions, the AI suggested live bill claiming, a ledger history, an offline travel mode, and counting chores as credit. We kept the first two. Offline sync adds heavy conflict handling, and pricing chores is a product question, not a cloud one.

**Main example: rejecting auto-scaling.** An earlier AI draft listed CPU-based auto-scaling as a main advanced feature. We questioned whether it fit. Our few requests per minute would never reach a CPU threshold, so demonstrating it would require fake load. Scaling the API would not help the weak point either: the single database. Losing ledger data is what would really hurt users, so we replaced auto-scaling with backup and recovery, and we will verify it by actually restoring a backup.

**Checking a technical claim.** The AI said `NOTIFY` is only delivered after commit. The PostgreSQL documentation confirms this and adds that sessions not listening at that moment never receive the message, so clients reload the bill on reconnect.

**How our design changed** (the open questions are answered in 2.3 and 2.5):

| Initial thinking | Final design | Why |
| --- | --- | --- |
| Store each member's running balance | Calculate balances from an append-only ledger | A stored balance can drift, and cannot show past balances or support restores |
| One API container | Two replicas sharing events through `LISTEN/NOTIFY` | Availability during restarts; a browser and its claims may reach different replicas |
| Restarting a container keeps its data | PVC on a DigitalOcean Volume, plus WAL archiving and daily backups | Pod filesystems are lost on recreation; a volume alone does not protect against deletion or corruption |





## Ledger Module

The Ledger module provides expense tracking, bill splitting,
balance calculation, settlement recording, and version history.

### Features

- Equal, exact-amount, and share-based splitting
- Expense creation using PostgreSQL
- Group balance calculation
- Suggested settle-up transfers
- Expense versioning with optimistic concurrency checks
- Soft deletion and restoration of expenses
- Historical balance queries
- Itemized bill finalization
- Settlement recording with idempotency protection

### Requirements

- Python 3.14
- PostgreSQL
- Dependencies listed in `requirements.txt`

### Setup

Create a Python virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Create the development database:

```bash
createdb splitsecond_db
```

Initialize the schema:

```bash
psql -X -v ON_ERROR_STOP=1 \
  -d splitsecond_db \
  -f backend/schema.sql
```

### Running Tests

Create a separate test database:

```bash
createdb splitsecond_test_db
```

Initialize the test schema:

```bash
psql -X -v ON_ERROR_STOP=1 \
  -d splitsecond_test_db \
  -f backend/schema.sql
```

Run all tests:

```bash
python -m unittest discover -s backend/tests -v
```

### Notes

- All monetary amounts are stored as integer cents.
- Positive balances indicate money to be received.
- Negative balances indicate money owed.
- Historical expense versions are preserved.
- Settlements record payments made outside the application.
- Database-backed tests use `splitsecond_test_db`.
- API authentication and authorization must be enforced
  before exposing Ledger operations to users.
```

## API, realtime and web UI

The FastAPI app in `backend/app/` adds sign-up and login (bcrypt + JWT), group-membership checks on every route, the live-bill claiming API, WebSocket updates across replicas through PostgreSQL `LISTEN/NOTIFY`, and a phone-friendly web UI served at `/`. See [docs/api.md](docs/api.md) for routes, the realtime protocol and how to run it.
