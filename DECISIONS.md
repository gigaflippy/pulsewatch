# Decisions

Open questions from nightly runs. Answer under **Georgi's answer** (one line is enough) and the next run acts on it first.

> Note (2026-10-24): the repository was renamed from `Firstproject` to `pulsewatch`. D-001 below keeps the old name because it records what was true when it was written.

---

## D-001 — What should nightly runs work on? (opened 2026-10-19)

**Status:** resolved — Pulsewatch v0.1 core built on `nightly/2026-10-19-pulsewatch-core`

**Context.** The nightly task is pointed at `gigaflippy/Firstproject`. That repo currently holds only `HELLO.md` (the push test from 2026-10-19). There is no code, no tests, no `main` branch (the default branch is `claude/hello-test`), and no description of what it is for. None of the side projects you've been working on (Pulsewatch, CharmMatch, Хранко, Огледалото, Canary, the drone project) exist as repos under `gigaflippy`. Picking a project and a stack for an empty repo would be guessing, so this run stopped here.

**Options.**

1. **Point the nightly task at a real project repo (recommended).** Push one of your top-priority projects (Pulsewatch or CharmMatch) to its own GitHub repo, even if it's rough, and change the scheduled task to target it.
   - + Runs do real work on code you actually care about: tests, bug fixes, features.
   - + Keeps each project's history clean and separate.
   - − Needs 5 minutes from you to create/push the repo and edit the scheduled task.

2. **Turn Firstproject into one of those projects from scratch.** Tell me which project and, if you have a preference, the stack (e.g. CharmMatch → Vite + React + TypeScript; Pulsewatch → Python service). Next run scaffolds it, adds tests/lint/CI, and builds from there.
   - + Zero setup on your side beyond one line here.
   - − Starts from nothing; any existing code/artifact you have would need to be pasted in or redone.
   - − Repo name "Firstproject" stays misleading unless renamed.

3. **Keep Firstproject as a sandbox.** Runs do small self-contained things only (README, a `main` branch, CI template, a repo-hygiene checklist).
   - + No decisions needed.
   - − Low value; mostly busywork.

**Recommendation:** Option 1 with Pulsewatch or CharmMatch. If you'd rather not create a repo, Option 2 — just name the project and stack.

**Also worth deciding (small):** create a `main` branch as the default? Right now the default branch is `claude/hello-test`, so PRs target that.

**Georgi's answer:** Pulsewatch (2026-10-19). Build it in this repo (Option 2).

**Spec (from Georgi's Pulsewatch project notes):** distributed uptime/latency monitor; flagship DevOps portfolio project; zero cost.
- Python/FastAPI probes + API, Docker, GitHub Actions CI
- Local Kubernetes (kind/k3d) instead of paid cloud; Azure/AKS dropped
- Terraform for the local platform, ArgoCD for deploys, Prometheus/Grafana for metrics

**Plan for next run:** scaffold the probe + API service (FastAPI, SQLite/Postgres-ready storage, pytest, ruff, Dockerfile, GitHub Actions). K8s/Terraform/ArgoCD come in later runs.

**Open, non-blocking:** if Pulsewatch code already exists on Georgi's laptop, push it here instead so runs build on it rather than starting fresh.

---

## D-002 — Terraform or OpenTofu for the local platform (roadmap item 5)? (opened 2026-10-20)

**Status:** resolved — Terraform (2026-10-20)

**Context.** Item 5 is infrastructure-as-code for the local platform: create the kind cluster, the namespaces and the monitoring stack from code instead of `make kind-up`. Since 2023, HashiCorp Terraform is under the BUSL 1.1 licence (source-available, not open source). OpenTofu is the Linux Foundation fork under MPL 2.0. It's a drop-in replacement: same HCL, same providers (`tehcyx/kind`, `hashicorp/kubernetes`), same workflow (`tofu plan/apply` instead of `terraform plan/apply`). Using either one for a personal portfolio project is free and allowed. The choice is about what you want the repo to say about you, and it's a licensing call, so it's yours.

**Options.**

1. **Terraform (recommended).**
   - + It's the name in job postings, Canonical's and Team.Blue's included. Recruiters and ATS filters search for "Terraform".
   - + Same skills transfer 1:1 to OpenTofu if an employer uses it.
   - − BUSL licence. That doesn't matter for personal use, but some people care on principle.
2. **OpenTofu.**
   - + Truly open source (MPL 2.0), and it's increasingly adopted, by Canonical-adjacent and open-source-first shops among others.
   - − Less recognised by name. You'd want "Terraform/OpenTofu" in the README and on your CV anyway.
3. **Both: write it for Terraform and test it in CI with OpenTofu too.**
   - + Shows you know the ecosystem split; the HCL is identical.
   - − A bit more CI time and one more thing to explain.

**Recommendation:** Option 1, with a README note that it runs unchanged on OpenTofu. If you care about open-source licensing, Option 3.

**Georgi's answer:** Terraform (2026-10-20): it's what job postings ask for. The README notes it also runs on OpenTofu.

---

## D-003 — Alert routing: Telegram, email, or both (roadmap item 7)? (opened 2026-10-21)

**Status:** resolved: Option 3 + Telegram (2026-10-21). Built on `nightly/2026-10-22-alertmanager`: Alertmanager routing + inhibition, local alert sink, Telegram switched on by `make alerts-telegram` / `.env`.

**Context.** Item 7 adds Alertmanager to route the existing alerts (`TargetDown` = page, `TargetDownInSomeRegions` / `TargetSlow` = warn) to a real channel. Every real channel needs a credential that only you can create. It must never be committed: it would live in a git-ignored `.env` for compose and in a Kubernetes Secret you create locally. Both options below are free.

**Options.**

1. **Telegram bot.**
   - + Free, instant, pushes to your phone. Setup is 2 minutes: talk to @BotFather, get a bot token, send the bot a message, read your chat id.
   - + Alertmanager has a native `telegram_configs` receiver, so there's no glue code.
   - − You need a Telegram account. The bot token is a secret you keep locally.
2. **Email over SMTP (e.g. a Gmail app password).**
   - + Every recruiter understands email alerts. Native `email_configs` receiver.
   - − Needs 2FA on the Google account plus an app password, which is a credential tied to your main mailbox. Slower, and alerts can land in spam.
3. **Credential-free first, real channel later.** Build Alertmanager now with the full routing tree (page vs warn, grouping by target, `TargetDown` inhibiting the region and slow alerts for the same target). CI tests it end to end against a local webhook sink. A Telegram/email receiver gets switched on only when a local Secret exists (`make alerts-secret TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=…`), so nothing secret ever touches the repo.
   - + Next run can start immediately; the interesting part (routing, inhibition, tests) doesn't depend on the credential.
   - − Until you add a token, alerts only reach the sink and the Alertmanager UI.

**Recommendation:** Option 3 with Telegram as the real channel (Option 1). Answer "3 + Telegram" (or "3 + email") and the next run builds it. You create the bot token yourself whenever you like. No run will ever ask for or store it.

**Georgi's answer:** 3 + Telegram (2026-10-21). Build the credential-free Alertmanager now (routing, inhibition, CI-tested against a webhook sink). Telegram is the real channel, switched on only when Georgi creates a local Secret himself. No run asks for or stores the token.

---

## D-004 — Which licence for the repository? (opened 2026-10-24)

**Status:** open, waiting on Georgi. Nothing else waits on this.

**Context.** The repository has no `LICENSE` file. On GitHub, that means "all rights reserved": people can read the code but are not allowed to copy, change or reuse it. For a portfolio project that is legal and fine, but visitors and recruiters often read a missing licence as "unfinished". Choosing a licence is a legal choice about your work, so it is yours. All options below are free.

**Options.**

1. **MIT (recommended).**
   - + The most common licence on GitHub. Short and easy to understand. Anyone can reuse the code if they keep your copyright line.
   - − No patent clause. That does not matter for this project.
2. **Apache-2.0.**
   - + Same freedoms as MIT, plus an explicit patent grant. Common in the Kubernetes and CNCF world.
   - − Longer text. It asks people who change files to mark their changes.
3. **No licence (keep as is).**
   - + Nobody may reuse the code without asking you.
   - − Looks unfinished to many visitors, and nobody can legally fork it to try it.

**Recommendation:** MIT, copyright "Georgi Georgiev". Answer with one word (MIT, Apache-2.0 or none) and the next run adds the file.

**Georgi's answer:**
