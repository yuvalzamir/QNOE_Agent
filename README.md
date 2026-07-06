# QNOE Lab Agent

A fully local AI agent for the QNOE group at ICFO Barcelona (PI: Frank Koppens). Runs on a NVIDIA DGX Spark on-premises — no data leaves the lab network.

## What it does

- Answers questions about group code, papers, and experimental data via Teams
- Searches 75K+ QCoDeS measurement runs by sample, experiment, date, or parameter
- Indexes 41 GitHub repos, the lab data server, and SharePoint document libraries
- Routes messages to sub-team agents (QTM, Photocurrent) with per-user profiles

## Stack

| Component | Details |
|---|---|
| LLM | Hermes 3 70B AWQ via vLLM, localhost:8000, 32K context |
| Agent framework | [Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.17.0 |
| Vector DB | Qdrant — 8 collections, ~380K chunks |
| Embeddings | nomic-embed-text-v1.5 |
| Interface | Microsoft Teams (polling adapter) |
| Hardware | NVIDIA DGX Spark GB10, 121GB unified memory |

## Structure

```
agent/          Python ingestion pipeline, watcher daemon, LangGraph code (superseded)
hermes/         Hermes Agent profiles, plugins, and config
  plugins/      qnoe_rag · qnoe_qcodes · teams_polling
  profiles/     qnoe-orchestrator · qnoe-qtm · qnoe-photocurrent
config/         Systemd service files, YAML configs (no secrets)
memory/         Obsidian knowledge vault — architecture decisions, runbook notes
runbook/        Operational scripts and ingestion runbooks
```

## Secrets

Copy `secrets/teams.env.template` → `secrets/teams.env` and fill in credentials. Never committed.
