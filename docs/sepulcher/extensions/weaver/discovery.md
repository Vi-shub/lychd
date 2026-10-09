---
title: Discovery and router delegation
icon: material/sign-direction
---

# :material-sign-direction: Discovery and router delegation

Discovery answers where to look next: which scope holds the relevant owners, which workflow fits
the operation, and which exact score can be considered for execution. Within
[Spellweaver](index.md), these routes help a coordinating Agent find the material needed to frame
a bounded task.

In the inner instrument, [Call](../../lich/call.md) gives this opening its address: relevant
parts of the work become reachable. [Blade](../../lich/blade.md) judges which material can
support the next act. A route can widen the question before a method narrows it.

This is one part of [discernment within exploration](../../lich/blade.md#discernment-within-exploration):
finding a source may reveal a better question, while a decisive distinction may redirect the
search. [Creation Inquiry](creation.md#reuse-inquiry-at-each-uncertain-boundary) carries that exchange
through bounded probes, independent sufficiency review, and Crucible.

[Documentation topology](../../../adr/01-doctrine.md#router-delegation) owns scope routing;
[Workflow](../../../adr/28-workflow.md) owns Pattern selection and task handoff. This page follows
those boundaries in use.

## How routers fan out

A router is an entry map with task triggers and explicit destinations. Some links end at an
owning document; others enter another scope tree whose router exposes more specialized branches.
The parent names when to enter that branch, while the destination maintains its deeper routes.
A branch and its downstream links form a discoverable subgraph of the documentation map.

Each route makes its purpose and destination legible:

| Destination | What the route exposes | Next act |
| --- | --- | --- |
| Scope or another router | Domain boundary, applicable triggers, and further routes | Follow the branch relevant to the task |
| Canonical owner | Architecture, vocabulary, operation, or delivery truth | Establish the governing contract and evidence |
| Workflow playbook | Procedure for the requested operation | Load it after the matching scope |
| Pattern or Scroll | A named score and its exact revision | Check registration and admission through Spellweaver |

Fan-out makes several branches available. The task determines which branches are read and in
what order. Enter another scope when the task crosses its boundary; leave unrelated branches
unloaded. A link to a workflow does not start it, and a link to a Pattern does not register or
authorize its casting.

## Specialized models for routing

Specialized decision models are promising candidates for reducing routing latency and cost when
repeated choices require semantic interpretation. For example,
[TypeSafe's Jev](https://docs.typesafe.ai/introduction) evaluates supplied state and typed questions;
its Choice primitive returns a selected option, probabilities, and confidence without generating
a prose answer. Such a model could propose a scope, registered Pattern, or declared graph branch,
leaving deeper reasoning for unresolved questions. This is an optimization to evaluate, not a
selected LychD dependency or a claim that the fastest router has been established.

Keep explicit commands and reliable deterministic matches as the baseline. For unresolved intent,
offer the model bounded destinations and an explicit uncertain or unsupported outcome. Supply the
relevant conversational context: a short confirmation can refer to substantial earlier work.
Clear proposals still pass normal validation and admission; ambiguity can trigger clarification,
another bounded probe, or deeper reasoning. A route proposal cannot rewrite an admitted Scroll.

Keep probabilities distinct from confidence. Jev's
[Choice confidence](https://docs.typesafe.ai/confidence) rescales the leading probability relative
to an even split across the options; `confidence = 0.9` does not itself mean 90% observed routing
accuracy. Check calibration and abstention on representative held-out cases, then set thresholds
according to the consequences of a wrong route. Compare end-to-end latency and cost, including
context preparation, network time, validation, fallbacks, and recovery, against rules and existing
model options at comparable task success.

Placement remains a separate choice. Jev's documented
[hosted API](https://docs.typesafe.ai/introduction/quickstart) is a remote disclosure boundary;
a local routing model requires its own verified deployment option. Sensitivity flags may inform
the [Privacy Cut](anonymization.md), but a remote router cannot receive private material merely
to decide whether that material may leave. Routing scores grant neither capability use nor egress.

## Follow a repository route

The repository already provides a concrete example:

1. [AGENTS.md](https://github.com/hexanomicon/lychd/blob/main/AGENTS.md) routes a browser task to
   the Frontend scope.
2. [Frontend](https://github.com/hexanomicon/lychd/blob/main/.agents/scopes/frontend.md) routes a
   component or reactivity task onward to the Svelte scope. It also exposes the backend scope
   when the change crosses the browser projection boundary.
3. The selected scope leads to the canonical owner and the smallest source and test slice needed
   to establish behavior.
4. If the task edits source, the root's workflow route exposes the
   [editing playbook](https://github.com/hexanomicon/lychd/blob/main/.agents/workflows/editing/index.md).
   That playbook routes onward to an applicable file-type card; Svelte-specific work continues
   through its scope's required workflow.

The root keeps Frontend discoverable; Frontend keeps its specialist routes discoverable; the
editing index keeps its procedure cards discoverable. Their contents remain with their owners.
When adding or moving a branch, maintain its parent link, task trigger, and destination together.
A missing prerequisite calls for a targeted probe or bounded request for the missing input.

## From discovery to a task handoff

Once the relevant owners and procedure are known, the coordinator can frame the task and select
the scope routes for its recipient. Under the designed
[scoped context handoff](../../../adr/28-workflow.md#scoped-context-handoff), those routes
travel with the task, acceptance target, operator constraints, allowed inputs and effects, exact
revisions, dependencies, and unresolved unknowns. The recipient then discovers further material
within that admitted boundary.

Opening another scope in the current conversation retains the material already loaded. A new
Agent handoff requires its own Context assembly. The parent keeps the wider purpose and assesses
the returned candidate and verification evidence; the
[bounded-task example](index.md#hand-off-a-bounded-task) follows that exchange.

## Discovering an executable score

For execution, use [Pattern lifecycle](pattern-lifecycle.md#identity-before-motion) to inspect the
registered exact revision and its activation policy. Admission pins the selected score; resume
uses that stored choice. A discovery link cannot redirect an admitted Run or splice a newly found
branch into its immutable Scroll.

The current [Loom](../../../state-of-the-work.md#loom-workflow-views) browses the fixed Pattern
registry. Scope routes and playbook links are repository navigation; they do not establish a
runtime discovery service or executable subgraph composition. General parallel fan-out and joins
retain the [Graph design boundary](../../../adr/24-graph.md#future-parallel-topology), and scoped
Agent handoff is not implemented by those repository routes.
