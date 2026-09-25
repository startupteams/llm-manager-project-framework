# Business Context

## Why LLM Manager exists

Startup Teams intends to operate persistent AI agents across local infrastructure and cloud providers. The infrastructure is heterogeneous: different GPU generations, CPU-only/unified-memory systems, multiple inference runtimes, patched model-serving stacks, model-specific parallelism, changing context/concurrency characteristics, and provider APIs.

The business problem is not simply “run a model.” The organization needs a repeatable way to answer:

- Which inference resources are available right now?
- Which model is actually behind a logical route?
- How much concurrency/context can a deployment support?
- What does it cost locally or through a provider?
- Can a known-good model configuration be restored safely?
- Which runtime/agent can be started, stopped, steered, or reassigned?
- How should ACMS request infrastructure behavior without managing GPUs and model servers directly?

LLM Manager centralizes those concerns.

## Business outcomes

### Reduce infrastructure risk

Known-good deployments should be captured before experimentation. History, benchmarks, and rollback reduce the chance that model research destroys a working service.

### Increase local-inference utilization

Model capacity and benchmarks make it possible to match agents to infrastructure instead of overusing expensive cloud inference or underusing local GPUs.

### Control cost

LLM Manager is intended to become the raw inference usage/cost authority. ACMS can use that data to make higher-level product/project decisions without duplicating infrastructure accounting.

### Support many persistent agents

Agent systems need more than one model request at a time. LLM Manager should evolve toward concurrency-aware allocation, load balancing, capacity reporting, and explicit queue/backpressure behavior.

### Preserve human control

Material model-policy changes can significantly change cost and behavior. They should be traceable and, where required, human-approved.

## Primary users

- Startup Teams infrastructure operators.
- AI agents performing authorized infrastructure/model work.
- ACMS as a machine client.
- Engineers and reviewers investigating model performance, capacity, and incidents.

## Non-goals

LLM Manager is not intended to become:

- a project-management system;
- a requirements-management system;
- the ACMS context store;
- the authoritative repository of agent transcripts;
- the human steering/co-work UI;
- a replacement for PDU Manager's authoritative electrical measurements.
