# SplitSecond: ECE1779 Project Proposal

| Team member | Student number | Email |
| --- | --- | --- |
| Jenny Qin | 1006960068 | yide.qin@mail.utoronto.ca |
| Cheng Zhao | 1012816954 | chan.zhao@mail.utoronto.ca |
| Fuchun Chai | 1003052082 | fuchun.chai@mail.utoronto.ca |

## 1. Motivation

Students split costs all the time: rent, groceries, trips, dinners. Two things tend to go wrong.

First, equal splits are often unfair. If one person orders a $14 noodle bowl and another orders a $38 steak and two drinks, splitting evenly is quick but unfair. Splitting fairly means passing the receipt around and working out tax and tip by hand.

Second, shared records are fragile. Groups keep track of debts in chat threads or spreadsheets, where entries get overwritten or deleted and nobody remembers why a balance changed. Apps like Splitwise help, but splitting by item still means one person enters and assigns every item, and a wrong edit is hard to undo.

SplitSecond is a shared expense ledger for roommates, travel groups, and friends. It adds two things existing tools lack. With live bill claiming, the payer enters the receipt once and everyone at the table claims their own items from their phone at the same time. With a ledger history, every change is kept, so users can see balances as of any past date and restore an expense that was edited or deleted by mistake.

This also fits the course well. A money ledger cannot lose or corrupt data, so persistent storage and backups are core requirements of the app rather than extras.

## 2. Objectives, Architecture, and Key Features

### 2.1 Core features

Users can:

- Sign up, create a group, and invite members.
- Add an expense split equally, by exact amounts, or by shares.
- Create a live bill from receipt items. Members claim items in real time; shared items are split among everyone who claims them, and tax and tip are split in proportion to each person's subtotal.
- See balances and a simplified settle-up plan (at most n − 1 payments for n people), and record payments.
- Browse the full history, view balances on any past date, and restore an edited or deleted expense.

### 2.2 Architecture

The backend is a stateless Python FastAPI service with a simple web UI. It runs as two replicas behind an nginx Ingress on DigitalOcean Kubernetes (DOKS). All state is in PostgreSQL, which runs as a StatefulSet with its data on a DigitalOcean Volume. A daily backup job copies the database to DigitalOcean Spaces.

### 2.3 Data model and correctness

The main tables are `users`, `groups`, `group_members`, `expenses`, `expense_shares`, `bills`, `bill_items`, `item_claims`, and `settlements`. A few design choices matter most:

- Money is stored as integer cents. $100 split three ways becomes 3334, 3333, and 3333 cents, and the transaction is rejected if shares do not add up to the total.
- The ledger is append-only. Editing an expense marks the old version as voided and inserts a new one. Balances are always calculated from the ledger, so they cannot drift, and the history view is just a query on `created_at` and `voided_at`.
- Each expense has a `version` number. If two people edit the same expense at once, the second save is rejected with HTTP 409 instead of silently overwriting the first.
- New expenses carry an idempotency key, so a retried request or a double tap does not create a duplicate.
- Each claim is a row with `UNIQUE(item_id, user_id)`, so claims can arrive in any order. Finalizing a bill happens in one transaction that checks every item is claimed and turns the claims into a normal expense.
- Every query is filtered by group membership, so users only ever see their own groups.

### 2.4 Required technologies

| Requirement | Plan |
| --- | --- |
| Docker and Docker Compose | One image for the API. Locally, Compose runs two API replicas, PostgreSQL, and nginx, so we test the multi-replica setup from the start. |
| PostgreSQL | Stores the ledger. We rely on its constraints and transactions for correctness. |
| Persistent storage | PostgreSQL StatefulSet with a PersistentVolumeClaim backed by a DigitalOcean Volume. Data survives pod restarts and rescheduling. |
| Deployment | DigitalOcean: DOKS, Container Registry, Volumes, and Spaces. |
| Orchestration | Kubernetes, with a Deployment and Service for the API, a StatefulSet and PVC for the database, a CronJob for backups, and an Ingress. We chose Kubernetes over Swarm mainly for StatefulSets and CronJobs. |
| Monitoring | DigitalOcean alerts on node CPU, memory, and disk; a `/metrics` endpoint on the API; and an alert if the last successful backup is more than 26 hours old. |

We are not planning auto-scaling. An expense app has little traffic, so two replicas for availability is enough, and we would rather spend the time on data safety.

### 2.5 Advanced features

1. **Real-time updates.** Live bills use WebSockets. Because a claim may be handled by a different replica than the one a browser is connected to, each replica listens on a PostgreSQL `LISTEN/NOTIFY` channel and passes events on to its clients. This avoids adding Redis. Notifications are only sent after a commit, and clients reload the full bill when they reconnect, since NOTIFY does not store missed messages.
2. **Backup and recovery.** The in-app history covers user mistakes. For infrastructure failures, a daily `pg_dump` CronJob uploads backups to Spaces, and we will write a restore script and test it by restoring and comparing balances. If time allows, we will add WAL archiving for point-in-time recovery.
3. **Security.** Hashed passwords, JWT sessions, group-level access checks, HTTPS with cert-manager, and credentials stored in Kubernetes Secrets.

A GitHub Actions CI/CD pipeline is a stretch goal.

### 2.6 Scope

Out of scope: multiple currencies, real payments, receipt scanning, and native mobile apps. The web UI will work on phones, which is all live claiming needs. We think the scope is realistic for three people because live bills and the history view both build on the same ledger code, and the hardest piece (point-in-time recovery) is optional.

## 3. Project Plan and Team Responsibilities

| Dates | Work | Deadlines |
| --- | --- | --- |
| Nov 4 – 10 | Ledger core, Compose setup | Proposal (Nov 7) |
| Nov 11 – 17 | Live bills, history view, minikube | |
| Nov 18 – 24 | DOKS deployment, backups, monitoring | Slides (Nov 24) |
| Nov 25 – Dec 5 | Restore testing, stretch goals, video, report | Presentation (Nov 25 or Dec 2), final (Dec 5) |

| Member | Responsibilities |
| --- | --- |
| Cheng Zhao | Ledger and data: schema, split and rounding logic, versioning, settle-up, history queries, bill finalization |
| Jenny Qin | Application: API, authentication and permissions, web UI, WebSockets |
| Fuchun Chai | Infrastructure: Docker, Compose, Kubernetes, storage, HTTPS, backups, monitoring |

Everyone shares code review, testing, the presentation, and the final report. In the first week, the ledger and application members will agree on the API models, and the infrastructure member will set up Compose so all of us work on the same environment. We will use feature branches with reviewed pull requests, and once a week we will deploy the main branch and run through the full demo.

The main risks are concurrency or rounding bugs, which we will cover with tests early; DigitalOcean cost, which we will limit by developing on minikube and only running DOKS from week three; and point-in-time recovery taking too long, which is why it is optional.

## 4. Initial Team Reasoning and AI-Assisted Exploration

### 4.1 Initial Team Reasoning

We started from our own experience. After a group dinner, one of us spent twenty minutes working out who owed what from a photo of the receipt, and the group chat still argued about the tip. We felt a useful tool had to split costs by what each person actually ordered, and keep a record that everyone in the group trusts.

Our first technical idea was to store each member's running balance in the database and update it every time an expense was added. We also assumed one API container and one PostgreSQL container would be enough, and that restarting a container would keep its data.

At the start, we were unsure about several things. How do we keep the database safe if the pod running it is deleted? If two people edit the same expense at the same time, whose change wins? How can several API replicas push live updates to the same group? How should rounding work when $100 is split three ways?

### 4.2 Exploration and Evaluation

We used an AI assistant to brainstorm topics that fit the course requirements. We first looked at an event check-in system, then switched to expense splitting because the check-in idea felt like something we would only build for the course. All three of us split rent and dinners every week, so we understand the users and can test the app on ourselves.

When we asked for more original directions, the AI suggested live bill claiming, a ledger history, an offline travel mode, and counting chores as credit. We kept the first two. Offline sync would add a lot of conflict-handling work, and pricing chores is more of a product question than a cloud one.

An earlier AI draft listed CPU-based auto-scaling as a main feature. We decided it did not make sense for an app with so little traffic and made backup and recovery the focus instead, since losing a money ledger is the failure that matters most.

The AI stated that PostgreSQL only delivers `NOTIFY` messages after the transaction commits. We checked the PostgreSQL documentation for `NOTIFY`, which confirms this. It also made clear that a session that is not listening at that moment never receives the message, which is why we added a full reload when a client reconnects.

We will document representative AI interactions and how we verified them in `ai-session.md`.
