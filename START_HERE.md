# MARSHAL - START HERE

This is the working directory for Mohith's **Microsoft Agents League Hackathon** entry.
A fresh Claude session should read this file and `REQUIREMENTS.md` first, then begin.
Persona: British English, no em dashes, address Mohith as "sir".

## The one-line state
Building a multi-agent **reasoning** assistant ("Marshal") on **Microsoft
Foundry (Azure AI Foundry)** for the Reasoning Agents track. Solo entry (Mohith +
Claude). Deadline **June 14 2026, 11:59 PM PT** (~08:00 June 15 BST). The tower /
Nex-lab project is ON HOLD; do not get pulled back into it.

**Build state (13 June 2026):** repo scaffolded; the Budget Governor and the full
reasoning loop (orchestrator, parallel workers, critic, self-correction,
synthesiser, guardrail) are built and unit-tested OFFLINE with a fake Foundry,
all green. The Foundry client wrapper is written against azure-ai-projects 2.2.0
and import-verified. Agent charters live in `prompts/`. Project venv at `.venv`.
CLI entry point: `python -m marshal_ai "question"`. Outstanding: wire the live
endpoint, then build the live UI (Phase 2) and the submission package (Phase 3).

## THE GATE - resolve this FIRST
The whole Foundry path depends on Azure access. Before any building, establish:
**does Mohith have an Azure account, student credits, or any Azure / AI Foundry
experience?** If yes or "let me sign up now", proceed to stand up Foundry (Phase 0).
If Azure genuinely walls in the first ~2 hours, fall back to the Enterprise track on
Copilot Studio.

**RESOLVED (13 June 2026):** Mohith has an Azure for Students subscription
(herts.ac.uk, $100 credit, valid to June 2027) and owns it, so the Foundry roles
are satisfied. Foundry path is GO. Standing up the project and deploying
gpt-5.1-mini. Only the live connection (project endpoint + `az login`) remains to
close Phase 0.

## What we are building
A self-governing multi-agent reasoning system, the proven "Nex lab" design re-homed
onto Foundry:
- an **ORCHESTRATOR** agent decomposes a hard question into sub-tasks,
- **reasoning SUB-AGENTS** work them (in parallel where sensible),
- a **CRITIC** agent grades each answer and triggers **SELF-CORRECTION** on weak ones,
- a **BUDGET GOVERNOR** caps spend and degrades gracefully,
- honest failure handling throughout,
- a clean **live UI** that visualises the swarm working.

The novel hook the judges will not see elsewhere: **governance + self-correction**.
Most entries are a single agent or a naive chain; ours has an orchestrator that
budgets and grades its own workers and fixes their mistakes.

## Why this track (the strategy)
Mohith's call: **attain the 90% we control (the expert-judged rubric), treat the 10%
community vote as a bonus we earn but never chase.** Reasoning/Foundry is the only
track where his real edge - multi-agent reasoning + reliability engineering - is
rewarded. His strength covers Reasoning (20%) + Reliability (20%) + Creativity (15%)
= 55% of the score. Soft spot is UX/Presentation (15%), so OVER-INVEST there.

## Rubric attack plan (see REQUIREMENTS.md for the weights)
- **Accuracy & Relevance (20%):** build ON Foundry front-and-centre; solve one concrete
  problem; hit every required artifact.
- **Reasoning (20%):** make the multi-step thinking VISIBLE in the demo - decompose ->
  workers -> critic -> self-correct -> synthesise.
- **Creativity (15%):** the self-governing, self-correcting orchestrator.
- **UX & Presentation (15%) [soft spot - over-invest]:** a clean "watch the agents
  work" view, a tight 5-min demo, a polished README and architecture diagram.
- **Reliability & Safety (20%) [our edge - flex hard]:** budget governance, graceful
  degradation, self-correction, input guardrails - and PROVE it in the demo by
  deliberately breaking something and showing the system recover.
- **Community vote (10%):** a sharp clip in the hackathon Discord near the end.

## Build phases (~1.5 days)
0. **Foundry stand-up (THE GATE).** Verify the CURRENT Azure AI Foundry Agent Service
   setup against live docs (it changes); get a "hello agent" running.
1. **Core loop.** Orchestrator + worker(s) + critic + self-correction, end to end on a
   real task. Function over polish.
2. **UI + reliability.** The live agent-swarm view + the budget governor + graceful
   degradation + guardrails.
3. **Submission package.** README, project description, architecture diagram (must show
   the Microsoft tools), and the 5-minute demo video (script + record).
4. **Submit + Discord clip.**

## Roles
Claude is the build partner: writes the agent code, generates the architecture diagram,
drafts the README and project description, writes the demo script. Mohith drives Azure
and records the demo.

## Blueprint to study (the proven design, in the tower repo)
The Nex lab is the working prototype of this architecture. Read these for the patterns
(orchestrator, worker dispatch, grading, self-correction, budget governance) and port
the IDEAS, not the PowerShell, onto Foundry:
- `D:\Personal AI Assistant\Jarvis-Reimagined\research\nex-foreman\FOREMAN_PROMPT.md`
  (the orchestrator's charter / grading + commissioning logic)
- `D:\Personal AI Assistant\tower-ops\foreman73_v2.ps1` (the full loop: allocate budget,
  commission via the orchestrator model, dispatch worker waves, file + digest results)
- `D:\Personal AI Assistant\tower-ops\nexdispatch79.ps1` (the dispatch half: budget clamp,
  parallel waves, blank-detection self-correction, honest failure handling)

## How to start
1. Confirm the Azure gate above.
2. `git init` this directory (it becomes the public submission repo).
3. Work the phases in order. Keep the README and architecture diagram growing as you build,
   do not leave them to the end.
