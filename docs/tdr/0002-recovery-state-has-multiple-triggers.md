# TDR-0002: Recovery State Has Multiple Triggers

**Status:** Accepted

## Description

The deployed recovery engine has historically considered desired service state and desired VM/power state separately. Setting a deployment to service-level maintenance alone has not always prevented the engine from restarting an intentionally stopped VM.

## Impact

Manual maintenance can be disrupted by unexpected recovery, especially during GPU passthrough handoffs. Operators/agents must understand two desired-state axes, increasing error risk.

## Possible Solutions

- Implement one transactional maintenance state that suppresses all recovery triggers.
- Add integration tests covering intentional VM stop, service maintenance, and recovery resumption.

## Owner

Startup Teams LLM Manager maintainers.
