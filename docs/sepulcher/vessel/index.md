---
title: Vessel
icon: material/skull-scan
---

# :material-skull-scan: Vessel

> _Through a mortal body, the daemon takes breath._

The **Vessel** is the application process through which LychD receives work and serves the
[Altar](../../divination/altar/index.md). Its composition root assembles domain services, admits
supported Invocations, and gives queued execution to [Ghouls](ghouls.md). The
[Lich](../lich/index.md) is the recurrent whole that may answer through this process and return
after it dies.

## One process takes breath

Granian supplies production ASGI; Litestar owns routing, injection, lifecycle, and API authority.
Pydantic validates configuration and typed boundaries. Pydantic AI supplies Agent and Graph
mechanics over [Animator](../animator/index.md) capabilities. Litestar also serves the compiled
static SvelteKit client: the browser projection acquires no separate server authority.

Exactly one ASGI process is required. Two SAQ workers, live Run events, cancellation, and the
service graph share its event loop. A second process or reload supervisor would create another
private runtime world. A blocking Ghoul can therefore delay HTTP as well as other work.

## Before the Altar opens

Startup connects queues, constructs services, warms the registry, and synchronizes standing
policy. With PostgreSQL, every required durable reconciliation pass must succeed before workers
or HTTP can observe the substrate; failure or degradation aborts startup. The memory profile has
no cross-process truth and retains best-effort recovery. Delivery, consent, and delegated-wait
relays carry repair forward after admission opens.

The owned launcher enters the application lifespan explicitly and runs one embedded no-lifespan
ASGI worker. It refuses new application callbacks once retirement starts and waits for existing
callbacks, including background work after response delivery, before closing application services.
Required startup or shutdown failure becomes a failed process exit. A signal requests this
retirement; the user manager's outer timeout still decides whether a stuck process must be forced.

The managed Vessel image uses the fixed internal `0.0.0.0:8000` listener behind generated
loopback-only host publication. Host-native Vessel deployment and hidden `lychd serve` are
retired. Images must be rebuilt to acquire launcher changes; unsupported server tuning refuses.

Current active installation lifecycle completion is unavailable until authenticated container
admission and exact settlement are implemented and qualified. The old signed native stops and
trusted installed-hybrid receipt remain frozen historical observations of their removed profile.
They do not qualify a rebuilt Vessel or authorize fallback control. Follow the current
[CLI boundary](../../state-of-the-work.md#core-cli-rites) before an installation operation.

Shutdown stops workers before their collaborators and queues. Process death loses active tasks,
subscribers, leases, and other volatile state. Only records committed through the
[Phylactery](../phylactery/index.md) can enter a supported return path.

Follow [Ghouls](ghouls.md) from admission to settlement, or
[Reanimation](../phylactery/reanimation.md) across process death.
[Backend](../../adr/11-backend.md) owns the architecture;
[State of Work](../../state-of-the-work.md#topology-a-local-runs) records the available local
execution and its evidence limits.
