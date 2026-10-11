---
title: Sentinel
icon: material/shield-sword-outline
---

# :material-shield-sword-outline: Sentinel

One proposed placement crosses from an inhabitant's intention into a server's world. Sentinel validates that crossing; the client adapter translates it. Structured server facts supply the evidence. Screenshots, signs, books, chat, and other world prose remain untrusted observations.

In `managed` mode, the server-side Sentinel can witness authoritative state and effects. In `attached` mode, admission is the intersection of local policy and explicitly offered server capabilities. Client observation cannot fill a missing server guarantee.

## The narrow crossing

Revision `2` retains revision `1`'s five reads and five effects. The managed fixture exposes all ten; an attached profile may offer fewer.

| Reads | Effects |
| --- | --- |
| `world_status` | `move_to` |
| `inventory_summary` | `look_at` |
| `scan_region` | `say` |
| `nearby_entities` | `equip` |
| `chat_since` | `place_block` |

Every action carries action/mission ids, bot UUID, server mode, Pattern/adapter revisions, lease, precondition digest, expected cursor, deadline, budgets, and postcondition. Managed Sentinel checks the plot, preconditions, cursor, and budgets on the authoritative server before issuance. Attached-mode local checks narrow the request; the external server still decides whether to accept it. A matching response plus either an authoritative event or sufficient resulting-state proof permits one durable receipt.

## Review the prepared crossing

Blockworld produces a typed placement review from the frozen command and trusted admission records. It binds its owner, profile revision, producer, admitted audience, and exact content to that command. The authorized reviewer can identify the mission and native action, server/world epoch and bot, plot and target, full block state, inventory change, relevant lease and limits, and the proof required for completion. Required facts that cannot be disclosed to the admitted audience make this profile unavailable. Generic Consent arguments remain censored; private checkpoint contents, arbitrary tool metadata, and model-written explanations cannot supply an alternative disclosure path.

The Consent record binds the exact preparation and review. Pause and resume retain that binding alongside the existing tool/effect definition binding. A missing or changed binding refuses issuance. Approval does not freeze the world: before the adapter is called, Blockworld rechecks current Principal and effect authority, policy generation, mission, server and bot identity, world epoch, lease, cursor, target, inventory, deadline, and budgets. Managed Sentinel enforces the applicable predicates at the authoritative server boundary. Attached-mode local checks cannot supply a guarantee the external server lacks.

## Refusal remains server-side

Revision `2` retains revision `1`'s exclusion of destructive edits, fire, lava, explosives, PvP, trading, private chat, cross-plot inventory, account/plug-in changes, restoration, and public exposure. Attached mode also excludes server configuration, console, lifecycle, snapshot, plug-in, and mod effects. A mistaken or hostile adapter cannot make server authority disappear.

If acknowledgement is lost, query `action_status` where available and compare the Sentinel cursor with resulting world state. Insufficient attached-server proof narrows eligible effects or returns `unverified_effect`. It never licenses another placement to obtain a cleaner answer. Socket, pathing, animation, and unacknowledged buffers remain volatile; [Continuity](continuity.md) owns durable re-entry.

Return to [Blockworld](index.md).
