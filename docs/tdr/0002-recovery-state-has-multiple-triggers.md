# TDR-0002: Recovery State Has Multiple Triggers

**Status:** Accepted

## Description

The deployed recovery engine has historically considered desired service state and desired VM/power state separately. Setting a deployment to service-level maintenance alone has not always prevented the engine from restarting an intentionally stopped VM.

**2026-09-25 source-verified correction:** in `service/app/v011_recovery.py` (`tick()`), `desired == "STOPPED_INTENTIONAL"` is the only leave-it-off path; any other deviation from desired power state fires `outage_detected → recovery_vm_start`, including plain `STOPPED`. `MAINTENANCE` only suppresses the HTTP-probe restart path (`maintenance_skip`). The web API (`v011_core.py`) additionally rejects any `desired_power_state` outside `RUNNING|STOPPED_INTENTIONAL`, and `power_off` requires `STOPPED_INTENTIONAL` first (409 otherwise). There is therefore no way to express "planned stopped" other than `STOPPED_INTENTIONAL` — and no unified maintenance transaction.

## Impact

Manual maintenance can be disrupted by unexpected recovery, especially during GPU passthrough handoffs. Operators/agents must understand two desired-state axes, increasing error risk.

## Possible Solutions

- Implement one transactional maintenance state that suppresses all recovery triggers.
- Add integration tests covering intentional VM stop, service maintenance, and recovery resumption.

## Owner

Startup Teams LLM Manager maintainers.
