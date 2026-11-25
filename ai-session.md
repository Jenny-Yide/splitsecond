# AI Session Record

AI influenced the project in three places: choosing the topic and features, reviewing our architecture, and checking how PostgreSQL NOTIFY behaves. Three representative interactions follow.

NOTE TO TEAM (delete before submitting): these are reconstructed from section 4 of our proposal. The course wants the real prompt and response copied from the chat history, so replace each Prompt and AI Response below with the original text (trimmed or summarized is fine). Do not submit paraphrases as if they were verbatim.

## Session 1: Choosing the topic and features

Prompt:

TODO: paste the real prompt (a request for project ideas, and the follow-up asking for more original directions for an expense-splitting app).

AI Response:

TODO: paste or summarize the real response. From our notes: the AI first suggested several topics, including an event check-in system. After we chose expense splitting and asked for more original directions, it suggested live bill claiming, a ledger history, an offline travel mode and counting chores as credit.

What Your Team Did With It:

- We kept live bill claiming and ledger history. We rejected offline travel mode because it needs heavy conflict handling, and chore credit because it is a product question and not a cloud one.
- The check-in idea felt like something we would only build for the course, so we switched to expense splitting, which we all use weekly.
- We made the choice in a team discussion after writing our own problem framing and open questions.

## Session 2: CPU auto-scaling

Prompt:

TODO: paste the real prompt (asking the AI to draft the architecture and advanced features).

AI Response:

TODO: paste or summarize the real response. From our notes: the draft listed CPU-based auto-scaling as a main advanced feature of the Kubernetes deployment.

What Your Team Did With It:

- It did not fit our system. With a few requests per minute the API would never reach a CPU threshold, so a demo would need artificial load, and scaling the stateless API does not protect the single PostgreSQL instance.
- We replaced it with backup and recovery: continuous WAL archiving with wal-g, daily base backups, a daily pg_dump and a `/health/backup` alert.
- We verified the replacement with restore drills on minikube. The crash drill measured an RPO of 107 s and an RTO of 66 s, and restored balances matched snapshots computed with the ledger's own functions.

## Session 3: When NOTIFY is delivered

Prompt:

TODO: paste the real prompt (asking how to push live bill updates to clients on different API replicas without Redis).

AI Response:

TODO: paste or summarize the real response. From our notes: the AI proposed PostgreSQL LISTEN/NOTIFY and said NOTIFY is delivered only after the sending transaction commits.

What Your Team Did With It:

- The suggestion gave us the design without Redis: one LISTEN connection per replica that forwards events to its WebSocket clients.
- The answer did not say what happens to listeners that are not connected. We read the PostgreSQL documentation, which confirmed delivery after commit and said that sessions not listening at that moment never receive the message. Payloads are also limited to 8000 bytes.
- We changed the design to match: payloads carry only IDs, clients re-fetch over HTTP, and clients reload the bill on reconnect. We checked it with `tools/e2e_two_replicas.py` and a 150-second WebSocket connection through the Gateway.
