# Deploy Patterns
*Last updated: 2026-07-13 (added "DGX ≠ master — code drift is real")*

> Standard procedures for deploying code and files to the DGX.
> Ownership pitfalls: [[memory/mistakes#M11 — DGX file ownership]] · Infrastructure: [[memory/infrastructure]] · Full setup: [[DGX_SETUP]]

## Standard Deploy Flow

1. Write file locally or to `/tmp/` on DGX
2. `scp` to DGX `/tmp/` if written locally
3. `sudo cp /tmp/file /opt/qnoe-agent/target/`
4. `sudo chown -R qnoe-ai:qnoe-ai /opt/qnoe-agent/target/`
5. `sudo chmod -R g+w /opt/qnoe-agent/target/`

## Restarting Services

```bash
# Agent (Docker container)
sudo systemctl restart qnoe-agent

# Watcher daemon
sudo systemctl restart qnoe-watcher

# vLLM — AVOID unless absolutely necessary (5+ min reload)
sudo systemctl restart vllm
```

## File Ownership Rules

- `/opt/qnoe-agent/` owned by `qnoe-ai:qnoe-ai` (uid 1001)
- User `yzamir` is in `qnoe-ai` group but NOT owner
- Always set group write: `sudo chmod -R g+w`
- Pre-create dirs Hermes might need at runtime

## Docker Image Rebuild

```bash
cd /opt/qnoe-agent
sudo docker build -t qnoe-agent:latest .
sudo systemctl restart qnoe-agent
```

## Checking Logs

```bash
# Agent logs
tail -f /opt/qnoe-agent/logs/agent.log

# Watcher logs
journalctl -u qnoe-watcher -f

# vLLM logs
journalctl -u vllm -f

# Nightly job logs
tail -f /opt/qnoe-agent/logs/nightly_reindex.log
```

## DGX ≠ master — code drift is real (mapped 2026-07-13)

The DGX does **not** track `master`. It runs a hand-deployed mix, and several feature branches are pushed-but-never-merged (per lab convention, merges to `main`/`master` need PI approval). Before deploying, **compare `md5sum` of the target file on the DGX against the intended source** — don't assume the repo == what's running.

Full audit + partial reconciliation done 2026-07-13 (branch `feature/sp-poller-reporting`). **CAUTION comparing md5:** the Windows working tree is CRLF but git blobs + the DGX are LF, so `git ls-files | xargs md5sum` vs DGX gives false-positive diffs. Compare `git show HEAD:<file>` (LF) to the DGX file, not the working tree.

Corrected drift map (direction matters — most drift was **DGX stale, git ahead**, NOT DGX hotfixes):
- **Bucket A — DGX stale, git ahead → deployed git→DGX (done):** `clone_org.py` (git HEAD sanitizes the GitHub PAT out of error logs; the DGX copy was logging raw stderr — a **PAT-leak**), `ingest_server.py` (missing `manifest_db=`), `run_ingest.py` (behind `4c8c490`), `teams_check.py` (whitespace only, skipped). My first pass wrongly called clone_org/ingest_server "uncommitted DGX edits" — they were just STALE.
- **Bucket B — live config never in git → captured DGX→git (done):** `config/gateway.toml` (openshell gateway JWT paths), `config/sandbox-policy.yaml` (the T4 sandbox policy from CLAUDE.md).
- **Bucket C — dead LangGraph cruft on DGX (REMOVED 2026-07-13, user-approved):** `agent/{graph,llm,main,prompts,retrieval,state,teams,tools}.py` + `agent/watcher/smb_watcher.py.bak`. Verified no live imports (runtime is Hermes, not `python -m agent.main`). **Soft-deleted:** copied to `/opt/qnoe-agent/.agent_trash/langgraph_legacy_20260713/` (9 files) then originals removed via `sudo find <path> -maxdepth 0 -delete`. Services stayed active; `agent` package still imports. **After this, DGX `agent/` == git tracked set (25 files).** Note: `sudo` NOPASSWD list is broader than older notes — it includes `find`, `ls`, `kill`, `journalctl` (so `sudo find … -delete` is how you delete/rm-equivalent, since `rm`/`mv` are NOT listed).
- **Bucket D — legacy deploy path, divergent (flagged, untouched):** `scripts/start_agent.sh` (DGX = `docker run`; git = `openshell sandbox`) — not the live path (services use start_hermes.sh / smb_watcher / start_llamacpp.sh; nothing runs `agent.main`).
- **Bucket E — DEFERRED per user (2026-07-13: "don't touch it for now"):** `hermes/` (700/qnoe-ai — can't `ls`/`find` as yzamir; needs `sudo cat` per known file or a perms window) and the unmerged branches `feature/{gpt-oss-cutover,mem0-per-user,context-pressure,gpt-oss-pilot}` whose work is deployed but never merged to master. True parity needs consolidating these into one committed baseline (PI-approval merge). **Leave until the user asks.**

**Root cause of drift:** deployment is manual (`scp`+`sudo cp`) with no link to git. Durable fix (proposed, not built): a deploy script that syncs a specific git ref → `/opt/qnoe-agent` so the box always equals a known commit.

**Pin-a-file-to-a-commit recipe** (which version is deployed?): `for c in $(git log --format=%h -40 <ref> -- <file>); do [ "$(git show $c:<file>|md5sum|cut -d' ' -f1)" = "<dgx_md5>" ] && echo $c; done`. Iterate over `git for-each-ref refs/heads refs/remotes` to search all branches, not just the current one.

**CRLF trap when deploying scripts:** the Windows working tree is CRLF; `scp` copies bytes exactly, so a deployed shell script gets a `#!/bin/bash\r` shebang → systemd `203/EXEC` crash loop. Deploy tracked files from the LF blob (`git show HEAD:<file>`), or `tr -d '\r'` an uncommitted file before `sudo cp`. Verify: `sudo cat <file> | tr -cd '\r' | wc -c` must be 0. See [[memory/mistakes#M48]].

## Onboarding a new agent user (access control)

Access is enforced by the gateway (`GATEWAY_ALLOW_ALL_USERS=false` + `GATEWAY_ALLOWED_USERS` in `scripts/start_hermes.sh`) plus the **`qnoe_authz`** plugin's notify-and-approve flow (see [[TODO]] "User allowlist", 2026-07-14).

- **Normal path (self-service, no SSH):** the new person DMs the bot once → an access request posts to the **Agent Logs** Teams channel with a ready `/approve <id>` line, and they get a "pending" reply. An admin (Yuval/Frank) DMs the bot **`/approve <id>`** (or `/deny <id>`). Approval lands in `hermes/pairing/teams_polling-approved.json`, which the gateway re-reads live — **no restart**. Other admin commands: `/pending`, `/revoke <id>`.
- **Permanent members ("floor"):** to add someone who must never be lockable (or a new admin), edit `GATEWAY_ALLOWED_USERS` (and `QNOE_ADMIN_USER_IDS` for approvers) in `start_hermes.sh`, redeploy (LF!), and restart. Floor members can't be `/revoke`d — they're env, not pairing store.
- **Also map their profile** (optional): add their AAD id → profile in `hermes/config/user_profiles.yaml` (unmapped users get `qnoe-orchestrator`).
- **Finding a user's AAD id:** it's the `user_id` in the gateway session store, the `WARNING Unauthorized user: <id>` log line, the `/pending` list, or Entra admin center → Users → Object ID. (The bot's app registration lacks `User.ReadBasic.All`, so Graph directory *name→id* search 403s; enumerating existing DM chat members via `Chat.Read` does work.)
