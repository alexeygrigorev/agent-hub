# Agent Hub

A small serverless hub (Cloudflare Workers Free + one SQLite Durable Object) that coordinates Alexey's multi-agent project: it launches a headless root run every 30 minutes on a random available host through the Agent Quota Launcher, runs the daily writer job, keeps the machine registry, carries messages between agents and machines (home machines connect outbound only), tracks the health of the principal and heads, and shows a dashboard. Each host runs a small outbound host agent.

Status: specification; implementation in progress. See [docs/SPEC.md](docs/SPEC.md) and [AGENTS.md](AGENTS.md).

License: MIT.
