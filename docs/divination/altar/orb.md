---
title: Orb
icon: material/crystal-ball
---

# :material-crystal-ball: Orb

Before trusting a result, find the Run that produced it and the limit of what was retained.
Looking through the **Orb** is **scrying**: inspecting ordered structural evidence with its gaps
still visible.

Open `/orb` for the parallel Run field, or choose **Look into the Orb** at a Bridge result to
inspect `/orb/{run_id}`. The shell’s **Orb** tab opens the parallel field; exact Run and event
links preserve the account being reviewed.

## Read the parallel field

The field starts with a bounded page of Run identities. **Load more retained Runs** extends the
catalogue; the number shown is the loaded count. Its newest page refreshes every five seconds
while visible. **Refresh Runs** rereads it immediately, preserving already opened Run lenses.
The canvas can display several workflows together, with at most three live Run lenses open.
Close a lens before unfolding another; its loaded Run group remains in the field.

Zoom out to see whole Run groups. Zoom in to disclose **Stations**, supplied **Occurrences**, and
**Records**, or choose those same levels directly. **Zoom changes detail** controls this automatic
disclosure. Each lens retains at most 100 records, and its coverage notice distinguishes that
window from older retained history. Current manifests supply no nested subworkflow identities;
an occurrence or delegated job is not an invented child workflow.

Select a Run to read its feed beside the canvas. **Follow selected Run** follows its projection;
manual camera interaction pauses following. Connection state and recorded Run status have separate
labels. **Open exact Run and history** enters the selected account below. The semantic Run list
offers the same identities and entry points without requiring canvas gestures.

## Establish the account you are reading

Confirm the **Run id**, **Latest fetched Run status**, and capture class in the header. The Run owns ledger
status. The Orb shows the bounded trace its sources can supply and cannot replace Run, consent,
grant, effect, or artifact truth.

**[Oculus](../../sepulcher/extensions/oculus.md)** names the body’s evidence service; **Orb** is
its Altar surface. Native durable Oculus storage is not delivered. Current evidence is available
through this running Vessel and is labeled `process local` or `durable best effort`.
`process local` evidence disappears when the Vessel process ends. With `durable best effort`,
PostgreSQL retains structural records, but failed writes and omitted token deltas can leave
sequence gaps.

Then read the coverage strip:

| Label | Boundary |
| --- | --- |
| **Read at** | Response assembly time. Run status, evidence and jobs are read separately; this is not an atomic historical state. |
| **retained through** | The retained ledger head. |
| **loaded through** | How far this page has loaded. |
| **live updates** | `ledger poll`: updates come from retained evidence, subject to its capture limits. |

**Refresh** requests a new bounded snapshot. The first page contains up to 100 evidence records;
**Load more retained evidence** requests another page while more remain. If that read fails,
the evidence already loaded stays visible; use **Retry loading retained evidence** to retry the
same page read.

The live feed reads newly retained structural records and metadata in bounded batches. It resumes
from its delivered cursor on reconnect, replaces a reset account through a fresh snapshot, and
settles when the ledger reports terminal status and the currently retained account has drained.
That settlement does not promise a complete trace. Token text, prompts, output bodies, and private
errors are absent. The live window keeps the latest 100 received records; older retained evidence
remains separately pageable. Refresh or a transport connection never changes the Run's status.

## Locate the event

## Locate the event

Use **Browse loaded evidence** to choose a **Recorded subject**. **Station observations** contain
NODE records; **Capability dispatches** contain DISPATCH records. A station and a capability with
the same name remain separate. Counts describe loaded records, not complete subject membership.
**Show all loaded evidence** restores the account. Filtering leaves reported gaps visible;
sequence jumps between filtered rows do not imply missing evidence.

For a station, choose a **Station attempt** to read records with that exact supplied correlation.
**No recorded attempt** keeps unassigned records visible. The raw `occurrence_id` field means
station-attempt correlation here, not a scheduled trigger Occurrence. Repeated attempts remain
separate, and an ID recorded under multiple station subjects is flagged without merging them.

Select a row to open **Selected historical evidence**. Its stable review URL is
`/orb/{run_id}?event={event_id}`. The inspector shows recorded sequence, producer timestamp, kind,
subject, phase, station-attempt correlation, and capture class. Its historical observation leaves
the header's latest fetched canonical Run status unchanged.

**Previous loaded record** and **Next loaded record** follow Run sequence across all loaded
records, including when timestamps repeat or move backward. Stepping outside a subject or attempt
filter restores the full account. The controls stop at the loaded boundaries; load another page
explicitly to continue. Sequence and timestamps establish neither elapsed work nor causality.

The subject and attempt selectors and record buttons use native keyboard controls. Opening a
record focuses the inspector; **Close** returns to its row when available, or to **Ordered
evidence** if filtering removed the opener. Changing a filter retains the selected record and
labels it when outside that filter. Refresh preserves the selected identity without moving the
reader's focus; if the record cannot be found at that read, the requested identity stays in the URL.

If the linked event lies beyond the loaded page, load more. If it is absent after the retained pages are exhausted, the Orb
reports that it was not found at this read. To investigate a wait, inspect its recorded status, phase, subject, station attempt, and
transition correlation. These observations may leave the cause unknown; the Orb has no
**Why waiting?** analysis. Follow [Stasis and return](../../sepulcher/extensions/weaver/stasis-and-return.md)
for the recorded wait's owner and return boundary, or
[Reanimation](../../sepulcher/phylactery/reanimation.md#reanimation-a-new-vessel-judges-durable-truth)
when the Vessel has died.

When delegated work exists, open **Delegated jobs** below the evidence and selected-record detail.
The disclosure shows at most the newest 32 summaries, each with
its `AgentJob` identity, runtime, Coffin profile, station, status, bounded-result presence,
artifact-reference count, and up to the latest 64 lifecycle events. Older omitted jobs and
truncation are explicit. A linked job absent from this bounded snapshot remains visibly unverified;
Orb does not choose another job in its place. Prompts, output text, and private errors are not exposed.

## Follow a relation only where it was recorded

**Bridge** returns to the owning conversation and focuses the exact Run’s turn when available.
An unavailable turn is reported. **Exact Pattern** opens [Loom](loom.md) only when the Run's pinned
manifest validates and equals the entire registered revision. The graph uses that same exact
declared score when available. Its Run and selected-event query preserves the return destination; neither
changes Pattern identity.

The fixed trusted source-browser [journey](../../state-of-the-work.md#current-evidence-envelope)
checks this exact Run→Pattern→Run relation and retained failed/useful history across a normal
reload. It does not establish complete trace capture, every evidence interaction or general
browser/accessibility acceptance.

A selected consent record with a retained reference offers **Open exact consent in Bridge**.
The reference records the historical wait; it does not establish that the request is still pending
or that you may judge it. Bridge focuses only the exact request for that Run in its current
conversation snapshot. A missing, settled, or unavailable request stays explicit; another request
is never substituted. **Return to Orb event** returns to the same Run and selected record.

A selected event with exact transition correlation offers **Open transition in Nexus** and
carries the selected event id. The link supplies a relation worth inspecting; viewing it requests
no mutation. [Nexus](nexus.md) keeps any lifecycle decision separate.

Selection, folding, and pagination leave Run state unchanged. Open `/orb` to compare another Run,
or follow it from Bridge. The current Orb offers no annotation, retry, cancellation, approval,
publication or transition action.

## Revisit a recorded moment

In an exact Run, **Record cursor**, **Previous record**, and **Next record** move through loaded
structural evidence. The graph shows observations through that sequence; future observations and
Animator selections disappear. Entering playback freezes that loaded lens, so incoming events and
live-window eviction cannot rewrite the moment being inspected. **LIVE** or **Follow latest**
returns to the latest loaded account. Refresh reads a fresh account and leaves playback.

The **Animators** rail names only identities supplied by retained dispatch records. Its stable
colours match dispatch nodes, and a glow marks a grant at the viewed cursor. A grant proves which
Animator was selected, not how long it was running or whether it remained available. Older records
without this identity remain unknown; current Nexus observations never fill in historical detail.
The parallel field shows these same supplied identities for its selected live Run.

Hover or keyboard focus previews a graph node's meaning without changing selection. Escape closes
the preview. Glow marks the latest record or playback cursor, not continuous execution. The Run
status above the graph remains current. Record spacing represents sequence, not elapsed time;
gaps, missing records, unretained tokens, and unloaded pages still limit the account. This is a
record cursor, not full-workspace wall-clock replay or a configurable retention service.

## Inspect a matching Archive selection

In an exact Run's **Archive selection** inspector, choose **Read Archive selection**. Nothing is
fetched until that gesture. **Refresh Archive selection** reads again; a failed read clears its
protected result and offers **Retry Archive selection**. Run navigation aborts and fences old
results independently of graph updates or playback. Read time and receipt creation time remain
separate. Long identities and digests open through native disclosures.

The separate same-host API read is
`GET /api/v1/archive/runs/{run_id}/recall`. Use the authenticated local API under the configured
Archive management authority; permission to view a Run alone does not disclose its source metadata.
The retained Run must identify the supported exact Bridge Archive Pattern and its admitted profile.

The response distinguishes `not_applicable`, `not_recorded` and `recorded`. A recorded receipt may
have zero selected notes; that successful empty selection differs from a failed or denied read.
It retains ordered source keys/revisions, exact record/lifecycle identities, scores, character and
result bounds and `omitted_for_budget`. These describe that selection: its character bound covers
selected note text, not the complete model Context or provider token accounting.

Capture is `matching_retained_request`. Recall accepts caller-supplied identities, so an exactly
matching receipt can exist before the workflow reaches recall. This read does not prove consumption
or provider transmission, reconstruct the released Context cache, or expose the prompt, source body,
answer or provider payload. Source names and digests remain protected local metadata.

A later correction, archival or revocation does not rewrite the old receipt. Current source
eligibility requires a separate authorized check; inspecting history neither resolves former
content nor makes that revision eligible again. Storage and binding failures remain errors rather
than an empty selection. [Memory](../../adr/27-memory.md) owns receipt authority and
[Observability](../../adr/29-observability.md#exact-run-archive-selection-explanation) owns the
capture boundary.

## Witness without possession

A richer view can reveal more of the retained account. It cannot fill the missing part of a
history. [Frontend's folding law](../../adr/15-frontend.md#folding-not-scale) keeps groups,
expansion, and sequence faithful to what their sources supplied.

## Reading direction

Place the selected account's qualifications where the reader judges it: Run identity, canonical
status, capture class, snapshot time, retained and loaded boundaries, and omissions stay beside
the graph and records. Summaries show the supplied subject; selection opens exact identity and
fields. Run-record timestamps include a date and use local time. A verified linked job opens its
disclosure and receives focus after the evidence and selected record, where a long job collection
cannot bury the account's limits.

The diagram must distinguish declared permission from supplied record membership and observed
traversal. Sequence and equal spacing establish order, not elapsed time or causality. Unknown
subjects and gaps stay visible; job events keep their own sequence unless the server supplies
a Run correlation. Fold counts describe loaded membership. Deeper expansion needs supplied
identities under the [folding contract](../../adr/15-frontend.md#folding-not-scale).

Acceptance case: open an event beyond the first page, distinguish not-yet-loaded evidence from
an unretained interval or failed read, inspect the exact score or correlated physical transition,
and return to the same selected event using only the keyboard. A narrow display should start
with a readable list. Efficient direct event seeking needs a separate retrieval contract; the
current path remains loading subsequent pages. The shared renderer's cross-engine lifecycle
qualification and wider accessibility acceptance remain recorded separately in
[State of Work](../../state-of-the-work.md#orb-instrument). A graph and live feed establish
neither complete capture nor execution control.
