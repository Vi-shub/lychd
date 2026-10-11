---
title: Delegated agents
icon: material/account-arrow-right-outline
---

# :material-account-arrow-right-outline: Delegated agents

> _The errand may leave the tower. The keys, the ledger, and the last word do not._

[Spellweaver](index.md) delegates through an attributed `AgentJob`. The coordinator tracks it; Graph
holds station state; the Run ledger binds its wait. An adapter receives a frozen request, never
the live Run, and the Pattern parks for one terminal adoption.

Today only the deterministic, effect-free `reference` adapter runs. Foreign CLI execution, Coffin
containment, and provider effects remain undelivered; [the delivered boundary](#name-the-delivered-truth)
below separates the current job mechanics from those designs.

[ADR 28](../../../adr/28-workflow.md#parallelism-and-delegation) owns delegation law.
[Agents](../../../adr/20-agents.md#streaming-history-and-limits) owns the opaque-runtime boundary;
[Workers](../../../adr/14-workers.md#delegated-agentjob-labor) owns `AgentJob` labor.

## Seal the labor

| Type | Exact contents |
| --- | --- |
| `DelegatedAgentRequest` | `request_id` (idempotency key), `run_id`, `step_id`, `runtime`, `profile` (`read`, `candidate`, or `verify`), `prompt`, and `input_artifacts` |
| `DelegatedAgentJobRef` | `job_id`, `request_id`, `run_id`, `runtime`, and `profile`; it does not copy `step_id` |
| `DelegatedAgentJob` | `request`, `ref`, `status`, optional `result`, and ordered `events` |
| `DelegatedAgentResult` | Same `job_id`, terminal `status`, optional `output`, `artifacts`, and `error`; `failed`, `timed_out`, and `lost` require an error, while `succeeded` forbids one |

An `ArtifactRef` is frozen metadata for external bytes, not bytes or custody. The full station
contract declares containment, budget, timeout, cancellation, artifact boundary, and permitted
downstream use. Today's
`DelegatedAgentRequest` has no budget or timeout fields, and State records no measured delegated
budget ledger.

`DelegatedAgentRequest` and `ArtifactRef` are frozen from construction. The same
`request_id` and content returns the existing reference without another start; conflicting
content fails. [Pattern lifecycle](pattern-lifecycle.md) owns the pinned workflow context.

## Park for one job

`queued → admitted → preparing → running → succeeded | failed | cancelled | timed_out | lost`

This is the normal path; earlier states may take only their declared terminal edges. The station
stores `job_id`, GraphRunner snapshots, and the worker binds that owner with
`AWAITING_DELEGATE`. Only its terminal job may re-admit the Run.

First terminal adoption wins. Duplicate or late result adoption is inert and appends no event; a
mismatched job id is rejected, and an older job cannot resume a newer wait. Only `succeeded` lets
the delegated rite project optional text; every other terminal status fails it.

Reference cancellation is idempotent; no effectful process-tree cancellation is delivered.
`lost` records indeterminate external truth: it is terminal for result adoption, neither polled nor
restarted, and not permission to repeat. It is also not containment proof. Explicit parent
cancellation still calls the owning runtime's cancellation operation and records
`lost → cancelled` only after that call returns. Cancellation during runtime start settles `lost`.
An unclassified exception after `runtime.start` is invoked also settles `lost`; a failure before
invocation may settle `failed`.

Resume admission atomically creates the fresh monotonic delivery key. Publication failure leaves
that exact queued delivery for relay. Startup recognizes the pre-park crash window only when the
first resumable checkpoint binds the same Run and delegated job; otherwise it contains correlated
jobs before parent failure. It refreshes durable delegated waits and re-admits only the terminal job
that owns each wait; missing coordination or owner identity fails required PostgreSQL startup.
After the admission CAS wins, repeated callbacks are inert.
[Stasis and return](stasis-and-return.md) owns re-admission.

## Keep the keys outside

The serialized envelope contains no `RunContext`, live toolsets, provider objects, leases,
credentials, or ambient authority. A profile and pure policy express posture; they do not prove
enforcement.

Returned text and `ArtifactRef`s are data, not authority. A lower-trust return remains
provenance-tagged and quarantined: it grants no instruction, execution, promotion, or effect
authority, only station-declared interpretation. A reference alone neither admits nor materializes bytes.
[Return quarantine](../../../adr/09-security.md#7-return-quarantine) owns this law.

The host marks candidate content produced through this job lineage `agent_generated`; a later
human approval does not relabel it. When a candidate touches a [Protected
Region](../../../adr/28-workflow.md#authorship-provenance-and-protected-regions), promotion follows
the exact live Gate in [Pattern lifecycle](pattern-lifecycle.md#human-attested-material-stops-at-a-gate).
The delegated runtime never receives the authority to alter or erase the target-owner manifest.

If a delegated request, child provider call, or returned derivative may cross a remote boundary,
it also follows the parent [anonymization and egress](anonymization.md) contract. Delegation cannot
copy authority or shed privacy lineage.

The runtime and its provider are separate consumers. A remote runtime receives its own minimum
workspace projection under a `DelegatedRuntimeTarget`; a child model receives a second consumer-
specific Cut when transformation policy requires one, plus a fresh `PortalTarget` decision for its
exact prompt/tool envelope. A local Coffin still
protects the host from its runtime even when only the child call crosses the network. The outer
`AgentJob` never authorizes an unknown series of child payloads.

This separation is also the delegation's economic leverage: the
[local anonymizer and bastion](anonymization.md#the-leverage-of-a-local-boundary) can make
subsidized remote cognition usable without making the remote runtime custodian of the raw
repository or its authority.

Provider-private commands and credentials stay behind the adapter boundary. Effectful foreign
labor additionally requires the Security-owned Coffin supervisor, job-scoped `nono` policy, and
Provider Gate; none belongs inside Pattern law. [The Coffin
profile](../../../adr/09-security.md#the-coffin-delegated-agent-profile) owns those boundaries.
A runtime that cannot use the exact Provider Gate endpoint, disclose its child model route, or
disable ambient network/fallback is ineligible. Transparent MITM is not an adapter.

## Wrap a CLI as a bounded adapter { #cli-adapter-passage }

Creation's [CLI branch](creation.md#cli-delegation-branch) uses this job boundary for a concrete
engineering assignment. The wrapper is a deterministic protocol adapter under a supervisor:
it translates an admitted task into one runtime invocation and translates observed termination
into a result. A separate native Agent may prepare or review that task. The wrapper does not
become another sefirah, coordinator, or source of permission.

The future adapter needs these five passages under the existing Coffin contract:

1. **Prepare.** Pin the base, task projection, allowed paths, runtime/adapter versions, output
   contract, limits, and authorized downstream use. Resolve effective configuration explicitly,
   including hooks, plugins, MCP servers, and continuation state; arbitrary project or user
   configuration must not expand the task. Apply the [Privacy Cut](anonymization.md#preserve-the-coding-question)
   when the chosen recipient requires transformation. Required governing instructions still reach
   the worker in its admitted packet.
2. **Launch.** Use an absolute executable and bounded argument vector, with task text supplied as
   data rather than interpolated shell code. Bind the provider session and supervised process to
   the job when observable. Attest workspace, environment, credentials, outbound calls, and resource
   boundaries before invoking an effectful runtime. Prompt instructions or CLI tool permissions
   alone do not establish this containment.
3. **Observe.** Parse the versioned machine event protocol, keep diagnostics distinct, and bound
   both streams. Preserve ordering, partial results, and capture gaps. An unknown usage count is
   unknown consumption, not a zero-cost receipt. Provider tool events remain attributed claims
   unless the trusted host independently observed the stated operation.
4. **Settle.** Correlate the terminal protocol record, process outcome, job identity, and retained
   artifacts. Truncated output, an unresolved child, or an ambiguous start cannot become success
   from the last sentence alone. Cancellation and timeout require the supervisor's full descendant
   settlement evidence; apply the existing `lost` and reconciliation rules when effects remain
   uncertain. Never start another attempt merely because the response was lost.
5. **Return.** Collect bounded candidate bytes under host custody, check allowed paths and base,
   and quarantine them separately from narrative and provider events. Use declared rehydration
   positions only; a raw patch is not a general substitution template. Independent local review
   and verification examine the exact resulting candidate. Adapter success neither passes those
   checks nor promotes the artifact.

For a first adapter, declare how each observed runtime event and exit condition maps to the
existing job statuses. Do not make every non-zero exit `failed`: work may already have occurred.
An adapter-specific rejection receipt can establish that nothing started; uncertainty cannot.
The current request/result types are a starting envelope, not evidence that this richer binding,
budget accounting, or artifact custody already exists.

### Compare actual integration surfaces { #cli-integration-references }

The following primary documentation was inspected for this design. It supplies implementation
leads, not LychD acceptance receipts or a reason to adopt a provider's default permissions.

| External surface | Useful mechanism | LychD still has to establish |
| --- | --- | --- |
| [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) | `codex exec --json` emits JSONL events; `--output-schema` requests a structured final result. | Exact event/terminal interpretation, effective configuration, artifact custody, and host verification. |
| [Claude Code programmatic mode](https://code.claude.com/docs/en/headless) | Print mode offers JSON, streamed events, and schema-constrained output. `--allowedTools` controls auto-approval; bare mode limits ambient discovery. | Tool availability and OS containment separately; required task context; pinned behavior when schemas, streams, or background work fail. |
| [nono's D-Bus advisory](https://github.com/nolabs-ai/nono/security/advisories/GHSA-27vp-2mmc-vmh3) | A documented local IPC escape in CLI profiles before the listed fix demonstrates why a sandbox name is insufficient. | Current patched implementation and policy, with negative tests for host IPC and descendant escape on the admitted platform. |

### Prepare the adapter before remote labor { #cli-adapter-acceptance }

Build the future capability in independently reviewable slices:

| Slice | Evidence needed before advancing |
| --- | --- |
| Protocol and result normalization | Recorded synthetic streams exercise success, explicit rejection, malformed/truncated output, duplicate terminal events, mismatched identity, and oversized diagnostics. No model or foreign executable is needed. |
| One contained local runtime | Observe denied out-of-scope reads/writes, unavailable host sockets and credentials, bounded output, complete cancellation, and reconciliation after an ambiguous launch. CLI flags alone are insufficient. |
| Admitted provider crossing | Verify the exact destination and payload, applicable Cut and utility evidence, and every child call's gate; block an undeclared retry or fallback. |
| Creation candidate return | An exact patch or artifact survives quarantine, any permitted rehydration, and independent checks against the real base. A useful proposal can still fail acceptance. |

Adversarial checks include a process that prints success before failing, a valid result whose
artifact belongs to another job or base, a background child that outlives its parent, and a patch
that targets a path outside the admitted workspace. Pair them with successful controls so refusal
cannot masquerade as a working adapter. These slices prepare future work; no CLI, sanitizer, or
new runtime adapter is implemented by this page.

## Name the delivered truth

Delegated execution is **Partial** in the [State of
Work](../../../state-of-the-work.md#delegated-agent-execution). The only runnable adapter is
`reference`: process-local, deterministic, and performing no model, filesystem, subprocess, or
network work.

Codex CLI (`codex-cli`), Claude Code (`claude-code`), and OpenCode Go (`opencode-go`) are
declared-only examples; none launches. No effectful
Coffin supervisor or CLI/provider process, credential isolation, or durable artifact custody is
delivered. Database shapes and a PostgreSQL store exist without a real provider or two-process
PostgreSQL recovery receipt; exact pre-park recovery is proved only with focused in-memory tests.

The [tracked delegated-coding
playbook](https://github.com/hexanomicon/lychd/blob/main/.agents/workflows/delegated-coding.md)
records repository procedure and design input. It supplies neither runtime law nor delivery
evidence.

[Execution roads](execution-roads.md) owns when this job form is preferable to a native Portal
call or sovereign A2A task.
