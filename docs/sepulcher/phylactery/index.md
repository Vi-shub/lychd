---
title: Phylactery
icon: fontawesome/solid/flask
---

# :fontawesome-solid-flask: Phylactery

> _The Vessel passes. What it inscribed waits in the dark._

When the [Vessel](../vessel/index.md) dies, another process must discover what happened before it
may continue. The **Phylactery** holds the committed records from which that judgment begins:
the PostgreSQL database cluster assigned to one application partition.

Domains give those records meaning and govern their lifecycle. Phylactery owns the engines,
codecs, transactions, migrations, and schema admission through which they are stored.

## What the inscription keeps

Different inscriptions answer different questions:

| Record | What a returning Vessel can establish |
| --- | --- |
| Run and delivery ledger | Canonical lifecycle and exact publication intent for each fresh or resumed worker hop. |
| `run_checkpoint` | One replaceable JSONB record per Run, containing validated Graph snapshot history and cascading with that Run. |
| Consent and delegated-owner records | The exact authority whose settled result may permit a parked Run to return. |
| Ordered `step` rows | Best-effort structural evidence; the Run ledger still owns lifecycle truth. |
| `session`, `karma`, `soulstone_record`, `codex_preauthorization` | Domain records admitted by application migrations; each row establishes only its own contract. |
| Public-note Archive | Explicit namespace ownership, note revisions and lifecycle, intake identities and text-free recall receipts; optional generation tombstones and latest derivation attempts. |
| SAQ `saq_*` tables | Durable broker state through a separate autocommit pool. Queue publication and Run admission remain separate transactions. |

Today these records use PostgreSQL's default schema and search path. [Persistence](../../adr/06-persistence.md)
owns exact table admission and migration history. Planned `vectors`, `traces`, and isolated `queue`
chambers describe governed memory, cognitive evidence, and broker separation; naming those
chambers does not deliver their services.

A checkpoint preserves declared Graph state. Subscribers, leases, provider handles, and
uncommitted frames die with the process; its image, runtime dependencies, and event stream cannot
be restored from that checkpoint. Terminal Run status commits before Context release and
best-effort checkpoint cleanup. A surviving checkpoint after failed cleanup grants no replay
permission. [Reanimation](reanimation.md) must first judge canonical truth.

## One cluster, one custody boundary

PostgreSQL runs in its dedicated unit. `PGDATA` occupies the exact admitted `postgres/data` mount
in the [Crypt](../crypt.md). The default design enlarges one cluster on one host; storage can move beneath
that mount without moving application authority. Process-local and in-memory profiles provide
bounded execution or test substitutes. Loose files cannot become a shadow database.

Growth begins with lifecycle policy. Age and disk pressure confer no deletion rights, and Shadow's
Reaper does not collect database records. Later measurements may justify partitions or
tablespaces that place hot and cold relations on local tiers while preserving PostgreSQL's one
query boundary. Extra mounts first need Layout, container, whole-cluster capture, and restore
support. No scale-out topology has been selected. A future named Domain store needs its own
custody or projection contract; it is not an interchangeable Phylactery backend. Follow the
[topology and scale seam](../../adr/06-persistence.md#default-topology-and-scale-seam) when that
choice becomes necessary.

## Read the evidence before claiming continuity

First-light persistence is **Partial**. The [named receipts](../../state-of-the-work.md#phylactery-first-light)
separate the lifecycle test with offline collaborators from actual warm-model reply/history
retention, including two normal source-CLI/native TCP boots. These source-profile receipts do not establish a
Consent-plus-Checkpoint restart, current installed body or crash recovery. A separate
[trusted source-browser journey](../../state-of-the-work.md#current-evidence-envelope) retains its
fixed useful reply after normal restart; its test composition remains explicit. The separate
[trusted installed-hybrid receipt](../../state-of-the-work.md#current-evidence-envelope) retains
identical complete SDK history, Intent capability and exact Pattern manifest across two normal
installed native boots; its private Docker and borrowed-model scope remains binding. General
retention, compaction and physical tiering are incomplete. The bounded
[public-note Archive](../../state-of-the-work.md#karma-semantic-memory) adds explicit intake,
lifecycle and lexical recall; old Karma rows remain inert. The independently
selected [derivation path](../../adr/27-memory.md#explicit-local-derivation) retains bounded vectors
and their invalidation fences. Its durable status reports only the latest attempt; expired work
requires an explicit retry. Separately selected semantic recall and Bridge revision 5 have fixed
native lifecycle and attributed-answer/empty-recall observations. Their exact model, authority
and source scopes remain binding; they do not establish general retrieval quality, automatic
curation, sharing or a formation path.

Follow [Ghouls](../vessel/ghouls.md) to see the workers that write these records, or
[Reanimation](reanimation.md) to cross the death of their process.
