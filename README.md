# Marshal

A self-governing multi-agent reasoning system, built on **Microsoft Foundry**.

> Microsoft Agents League Hackathon, Reasoning Agents track. Solo entry.

## The problem

Most agent demos are a single model in a loop, or a naive chain that runs once and
hopes the answer is right. They have no idea how good their own work is, and no
limit on what they will spend getting it. Marshal is built around the
opposite idea: an orchestrator that **budgets and grades its own workers, and
fixes their mistakes**.

## What it does

Give it a hard question. It:

1. **Decomposes** the question into scoped sub-tasks (the **Orchestrator**).
2. **Dispatches** sub-agents to work them in parallel (the **Workers**).
3. **Grades** every answer and re-commissions the weak ones (the **Critic**, driving **self-correction**).
4. **Synthesises** the graded results into one answer.
5. Runs all of it under a hard **Budget Governor** that degrades gracefully rather than failing.

You watch the whole swarm work, live.

## Why it is different

The novel hook is **governance plus self-correction**. The system is honest about
failure (blank and thin answers are detected and re-scoped, not hidden), it never
overspends (a reserve is always held back so it can still answer), and it proves
its own reliability in the demo by having something deliberately broken and
recovering from it.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design and diagram.

Built on **Microsoft Foundry Agent Service**: each role is a Foundry prompt agent,
created with the Foundry projects API (`azure-ai-projects` 2.x) and driven through
its OpenAI-compatible Responses API. All reasoning runs on models deployed in
Foundry.

## Tech stack

- **Microsoft Foundry (Azure AI Foundry) Agent Service** for the agents and models
- Python, `azure-ai-projects` >= 2.0.0, `azure-identity`
- Live UI: to be added in the UI phase

## Running it

Outline (the entry point is wired once the Foundry endpoint is available):

1. `pip install -r requirements.txt`
2. `az login`
3. Copy `.env.example` to `.env` and set `PROJECT_ENDPOINT`.
4. Run the reasoning loop on a question.

## Project layout

```
src/marshal_ai/
  config.py     settings, model roles, approximate pricing
  budget.py     the Budget Governor (graceful degradation)
  foundry.py    thin wrapper over the Foundry Agent Service API
docs/
  ARCHITECTURE.md   full design and the Microsoft-tools diagram
```

## Submission checklist (Reasoning Agents track)

- [ ] Public GitHub repository with source code and README (this repo)
- [ ] Project description (features, functionality, problem solved, technologies)
- [ ] Demo video, 5 minutes maximum, on YouTube or Vimeo (link to follow)
- [ ] Architecture diagram showing use of the Microsoft tools
- [ ] Microsoft Learn username

## Status

Early build. The repo, the budget governor, and the Foundry client wrapper are in
place. Next: the orchestrator, worker, and critic agents, the control loop, and
the live UI.
