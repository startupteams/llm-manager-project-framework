# Model Serving, Benchmarking, and Rollback

## Principles

1. The actual served model identity must be known.
2. A model benchmark is meaningful only with its exact runtime/hardware/configuration.
3. Known-good state is preserved before experimentation.
4. Experimental runtimes are isolated from production runtimes.
5. Long-context and agent-tool behavior must be qualified separately from simple HTTP health.

## Deployment revision contents

A reproducible deployment revision should capture, when applicable:

```text
model artifact / revision / checksum
served model name
quantization
runtime / version / commit / container digest
site / host / VM / IP
GPU type/count/VRAM
CPU / RAM / RAM speed when available
context
TP / PP / DP / EP
max_num_seqs / batching
GPU memory utilization
KV cache dtype
prefix caching / chunked prefill
speculative decoding / MTP
CPU offload / PLE configuration
launch command / environment
systemd unit / service identity
routes / aliases
status and notes
```

## Benchmark record

Store at least:

```text
deployment_revision_id
benchmark profile/version
prompt tokens
output tokens
concurrency
TTFT
per-agent output tok/s
aggregate output tok/s
latency
success/errors
tested context
RAM/VRAM
hardware/runtime metadata
Hermes/tool-smoke result where relevant
raw result path
```

## Suggested quick qualification

- C1 short prompt (~1K input / 128 output).
- C1 medium prompt (~8K input / 128 output).
- C4 and C8 when appropriate.
- Long-context acceptance around the intended operating context.
- Hermes/tool-call smoke for agent-serving routes.

Do not force one benchmark profile across a deployment that cannot support it safely; record the reason.

## Same-node versus cross-node testing

For model comparison, prefer testing models serially on the same host. For host comparison, run the same model/configuration on each host before tuning separately. This separates model effects from RAM speed, CPU/NUMA, PCIe, cooling, and host-specific behavior.

## Restore workflow

Historical configuration restore is intentionally two-stage:

1. **Load Configuration** — copies a history revision into pending configuration without service impact.
2. **Restore & Serve** — separately confirmed state-changing action that applies the pending revision, validates it, and records the outcome.

## Current custom-runtime reality

Some models require patched/community runtimes that differ materially from stock vLLM. These are supported operationally only when their exact recipe, image/commit, patchset, artifact revision, and launch configuration are captured.

A representative example is Qwen3.8-Flash-Next on MIAM-00111, which was proven with a pinned PP-enabled Ampere vLLM stack using TP2×PP3, expert parallelism, FP8 PLE CPU offload, and six RTX 3080 GPUs. That working recipe must be treated as a specific deployment revision, not evidence that arbitrary upstream vLLM versions will work.
