---
title: 30. Webcrawler
icon: material/spider-thread
---

# :material-spider-thread: 30. Webcrawler

!!! abstract "Context and Problem Statement"
    Web acquisition crosses an untrusted network and returns untrusted bytes. Search, fetch,
    extraction, crawling, rendering, interaction, credentials, sessions, and storage are distinct
    effects; failure may never authorize a stronger one. [Scout](../sepulcher/extensions/scout.md)
    binds each acquisition attempt to its admitted effect and retains its source attribution.

## Decision

Scout is the acquisition Extension Domain, not a crawler service or a Composition. [The first passage](#the-first-passage) follows one bounded static read from proposed URL to attributed text. A provider
supplies mechanism only; the host binds an Agent's or admitted application Pattern's proposed
locator to principal, Run, effect, destination scope, consent, provider, and budget. Scout owns
acquisition mechanics, attribution, freshness, and destination/contact policy—not an application's
source-eligibility judgment, interpretation, truth, autonomy, or consequence.

Each of **Search, Fetch, Extract, Crawl, Render, Interact, Credential Use, Session Custody,
Screenshot,** and **Download** needs its own host-owned `ScoutEffectGrant` and ceiling. **Artifact
Admission is a separate custody-owner effect**, not an eleventh Scout track: Scout may hand off the
quarantine identity and acquisition receipt, but the custody owner validates classification,
retention, retrieval authority, and exact bytes, and alone returns the durable artifact reference.
Neither the application eligibility decision nor a Scout grant can pre-approve that custody
judgment.

Authentication, CAPTCHA, payment, quota, robots denial, provider failure, cancellation, or a
challenge is a terminal typed result: no identity rotation, provider switch, retry, spend, or
effect escalation follows. No-web profiles are valid and confer no ambient egress.

### Provider, Animator, and effect-grant boundary

A Scout **Provider** names one admitted acquisition mechanism. An [Animator](../sepulcher/animator/index.md)
may manifest that mechanism as a local Soulstone or remote Portal, but it does not replace Scout:

- the Animator Rune, adapter, probe, and capability declaration establish what service exists and
  whether it is ready;
- the Scout provider binding declares the exact acquisition effects that service may implement;
- a Dispatcher `CallGrant` or `JobGrant` exposes only the exact technical surface and lease; a
  separate `ScoutEffectGrant` authorizes each Search, Fetch, Extract, Crawl, Render, or other effect;
- a provider call that necessarily combines effects may run only when every constituent effect grant is
  present. A convenient scrape endpoint never merges those grants into one authority.

An Animator-owned `ToolConnector` therefore cannot expose a provider's raw SDK, MCP server, REST
surface, CDP session, or browser automation API directly to an Agent. The Agent-facing tool is
Scout-owned and host-mediated: it validates the current effect grants, sends one adapter-authored bounded
request, normalizes the response, and settles the Scout receipt. Animator registration without an
effect-specific Scout provider contribution grants no acquisition authority.

The designed contribution binds one provider identity/revision, host-assigned Registrant,
semantic interface and operation, exact required effects, immutable profile and dialect-driver
references, and conformance evidence. Its execution binding selects either a native adapter or
one exact Animator capability. These references resolve before the Scout-owned store seals with
the [extension assembly](05-extensions.md#2-the-registration-surface-the-extension-context).
Conflicting ownership, unresolved references, and post-assembly mutation refuse. Scout binds the
[Dispatcher's interface/profile/driver contracts](22-dispatcher.md); it does not create another
capability registry or disguise Search as a model.

Configuration selects an admitted binding and may narrow its ceilings. It cannot contribute code,
an endpoint, extra engine, or new effect through request parameters. Preparation retains the
binding and effective configuration digests; replacement requires a new admitted attempt, and
executable changes follow the existing Vessel-generation boundary. A readiness probe checks its
declared local service surface only. A real search probe needs its own Search and egress admission.

### First-party mechanism policy

LychD's first-party web path must remain useful without a paid remote service. The accepted
mechanism sequence is:

| Mechanism | Concrete form | First-party office |
| --- | --- | --- |
| Native static passage | trusted local Scout code with a disposable, network-denied parsing/extraction child | one bounded public-HTTPS Fetch followed by network-free Extract |
| [SearXNG](https://github.com/searxng/searxng) | digest-pinned Soulstone behind a native adapter | selected Search provider; locators and attributed snippets only |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | digest-pinned, separately isolated Soulstone | experimental candidate for one exact public-HTTPS Fetch + Render + Extract passage |
| [Firecrawl](https://github.com/firecrawl/firecrawl) self-hosted | no selected concrete form | deferred comparison candidate, admitted only if it later proves a material advantage over Crawl4AI under the same effect and containment contract |
| [Browserless](https://github.com/browserless/browserless) | no selected concrete form | no first-party raw CDP, Playwright, function-execution, or download adapter; future comparison must satisfy the same narrow renderer contract |
| Paid web-acquisition API, including Tavily and hosted crawler/browser services | operator-owned private Portal extension | no first-party built-in adapter or automatic compatibility promise |

SearXNG is Search, not Fetch. Its result URL remains an observation and needs a new Fetch effect grant
before contact. Its engine catalogue, parsers, suspension policy, and upstream maintenance remain
inside the separate service; LychD owns the Rune, exact JSON profile, ingress authentication,
enforced egress mediation, result normalization, probe, and conformance suite. The
[Search contract](#searxng-search-contract) below defines that mediation; an ordinary SearXNG
container does not already satisfy it.

Crawl4AI's name grants no Scout Crawl authority. The initial candidate has no frontier, arbitrary
JavaScript, hooks, LLM extraction, credentials, cookies, persistent session, screenshot, download,
artifact custody, or generic MCP surface. It receives one exact URL only after the host holds the
separate Fetch, Render, and Extract effect grants. Promotion requires an authenticated narrow API, an
immutable image, a sandboxed browser, a dedicated renderer containment zone, independently gated
destination egress, strict time/process/byte/output ceilings, a license/SBOM receipt, and
real-browser adversarial tests.

Firecrawl is not carried alongside Crawl4AI merely as optional duplication. Its self-hosted stack
remains deferred while it couples more acquisition effects and operational services than the
selected passage needs. Reconsideration requires evidence of materially better coverage,
reliability, or containment at comparable authority and operating cost; cloud Firecrawl remains a
paid private-Portal concern under the rule above.

Browserless is not selected as a second browser backend. A broad CDP or Playwright connection hands
the caller more authority than the first renderer passage, while a service-specific REST facade
still needs the same Scout effect grants, destination gate, containment, and output limits. It returns to
comparison only for an identified gap that Crawl4AI cannot satisfy; adapter multiplicity is not a
goal.

### Attempt layers and crash semantics

The Scout-owned effect attempt carries principal, Run, exact effect grants, destination, provider
binding, budget, acquisition disposition, and receipt semantics. A bounded synchronous Scout
effect enters `prepared` before I/O. An Animator-backed implementation additionally holds a scoped
`CallGrant`; a native host-owned adapter pins its exact Scout provider/adapter binding and uses no
Dispatcher lease. Neither creates a `ServiceJobAttempt@1`. If the process dies before settlement,
Scout records `unknown_after_crash` and does not replay the effect without independent evidence.

An asynchronous crawl or render admitted through `JobGrant` keeps that Scout domain job and layers
it over Core's [`ServiceJobAttempt@1`](14-workers.md#service-job-attempts). Its
`unknown_after_crash` domain disposition maps to the mechanical attempt's `INDETERMINATE` state;
recovery reconciles the same capability-backed execution binding and never invents a second
submission.

No provider is a fallback. One prepared Scout effect attempt binds one selected provider and
profile. A static page that needs JavaScript, an exhausted Search provider, or a renderer failure
settles with that outcome; another provider or stronger effect requires a newly admitted attempt.

### Native adapters and upstream services {#integration-not-vendoring}

A **native adapter** means LychD-owned Rune schemas, provider binding, probe, request allowlist,
normalizer, receipts, and conformance tests. It does not mean copying a search engine, crawler, or
browser service into the MPL-covered core. Upstream services retain their own source, image,
license, release cadence, and process boundary. Small useful patterns may enter through
[Assimilation](35-assimilation.md) with provenance and independent expression; entire engine
catalogues and service internals do not.

## SearXNG Search contract

The designed `searxng-search@1` profile supports one query, one qualified engine, one result page,
and at most ten locator observations. A later multi-engine profile must qualify each recipient
and their aggregate disclosure budget. The Agent proposes query text, a supported language/time
filter, and a result limit within the profile. The host owns provider selection, engine identity,
endpoint, credentials, request parameters, deadlines, and grants. Unknown fields and unsupported
filters refuse before contact; an arbitrary provider-options dictionary is not part of the tool.

The adapter uses authenticated `POST /search` with explicitly enabled JSON output, no caller
cookies or preferences, and no redirect following. The effective service configuration positively
selects the engine; inherited defaults, autocomplete, favicon retrieval, answerers, and optional
plugins cannot add contacts or outputs. Query syntax that selects engines, categories, languages,
external bangs, or lucky redirects is refused by the pinned parser contract, not guessed away
with an unqualified regular expression. Typed filters are compiled only where that engine proves
support. These restrictions also apply at service admission and transport, independently of input
validation. The local service address comes from its registered control-plane binding; it does
not grant an exception for public acquisition to contact private destinations.

### Query disclosure and engine transport

Query derivation preserves [Context influence and privacy labels](21-context.md#privatization-and-the-privacy-cut).
A local search service still discloses to remote engines. Before each upstream transmission,
[Security](09-security.md#portal-privatization-and-egress) must admit the exact final request:
recipient and custody route, canonical target and wire digest, source influence, disclosure basis,
policy, expiry, and remaining budget. Raw-eligible public queries still require this decision;
URL encoding is not anonymization. The local ingress request never authorizes unknown fan-out.

The selected integration requires an explicit cooperative request-mediation seam: the service
prepares a bounded engine request, the trusted transport admits its final bytes and sends it, and
the service receives the bounded response for parsing. Independent containment denies direct
service egress. Query-bearing URLs, headers, bodies, initialization, auxiliary requests and
redirects must all cross that seam under an exact admitted contact plan. No transparent TLS
interception, ambient proxy, destination allowlist alone, or after-the-fact observation substitutes
for the Gate. The adapter must account for transport-generated fields before sealing the request.
No compliant upstream hook, engine profile, image, or containment implementation is claimed here.

The first profile has no retry, redelivery, proxy/identity rotation, page expansion, or fallback.
It reserves a finite contact budget before preparation; every transmission consumes both that
budget and the purpose's disclosure allowance before its first byte. A failed engine remains a
failed engine. A disconnect, quota, challenge, or `Retry-After` cannot authorize another request.
An engine requiring an unmediated call is ineligible. Cancellation fences new transmissions and
requires evidence that owned work stopped; an uncertain terminal state follows Scout recovery
law. Raw queries, query-bearing URLs, provider errors and unkeyed content digests stay out of
general logs, Orb and Loom. Their permitted projections follow Security's restricted-evidence law.

### Search observations and failure meaning

The normalizer returns host-authored attempt/provider/profile references, observation time,
normalizer identity, bounded ordered results, coverage, declared omissions, and separate measured
and provider-reported usage. Each result carries original locator, any derived normalized locator,
rank, plain title/snippet, and provider-reported engine attribution. Normalization preserves query
parameters and path meaning; canonicalization never creates permission to contact the URL.
Provider dates, scores and attribution remain claims, not verified freshness, confidence or truth.
Deduplication retains the contributing identities and ranks. Raw-response and derivation digests
remain restricted local evidence, not public telemetry.

Result count and coverage are independent. Coverage is `no_reported_failure`, `partial`, or
`unknown`; the first means only that the provider reported no failure. Zero results with a failed
or suspended engine is not a clean miss, and an empty error list does not prove every intended
contact completed. Unrecognized engine errors map to a generic attributed failure; translated
provider prose never becomes a stable protocol code. Partial observations are permitted only for
provider-reported engine failures within an otherwise valid admitted exchange. Host authority,
destination/egress, adapter-to-service transport, framing, schema, resource, or cancellation
failure is terminal and cannot be salvaged into successful partial output, even if some results
arrived before the refusal. Invalid required locators refuse the response rather than silently
manufacturing an empty result. A declared top-ten selection is an intentional projection; a byte
ceiling failure is a refusal, not permission to truncate a record.

Generated answers, raw page bodies, images and extra links are outside this profile. The
Scout-owned tool returns attributed data to Context. Fetching a result, retaining its bytes, or
submitting a memory candidate requires the respective owner to admit a separate effect.
[Memory](27-memory.md) keeps candidate intake and explicit promotion; search relevance cannot
grant recall eligibility or instruction authority.

## The first passage

The first designed passage is one unauthenticated static HTTPS GET and network-free extraction:

1. An Agent or admitted application Pattern proposes one exact URL. The host validates its
   principal and Run, admits separate Fetch and Extract grants with their own ceilings, and binds
   the Fetch destination. It selects an eligible static provider, reserves worst-case budget, and
   durably records those exact grants in one Scout effect attempt as `prepared` before I/O.
2. The provider pins and performs one GET with only explicitly bounded, re-gated redirects: no
   subresources, JavaScript, cookies, cache, URL credentials, `.netrc`, ambient proxy, custom
   headers, automatic retry, or rendering.
3. A network-free extractor accepts only allowed HTML, XHTML, or plain text; it records raw and
   output digests, extractor identity, and loss, then returns fenced attributed text.
4. Settlement records disposition and usage. A prepared bounded-call Scout effect attempt after a
   crash is `unknown_after_crash`; without an independent terminal record there is no blind retry.

Wire bytes, decoded bytes, expansion ratio, parse work, output, headers, time, redirects, rate,
concurrency, depth, requests, and spend are separately bounded. `Content-Length` and
`Content-Type` are hints. Mismatch, truncation, unsupported media, or any limit failure refuses.
Crawl, when admitted, adds a finite deduplicated frontier, page/depth budget, per-origin
scheduler, identifying agent, and fail-closed robots evaluation; robots is neither authentication,
site terms, nor legal permission.

### Initial qualification envelope

The designed `static-public-page@1` envelope below is a conservative qualification target, not a
measured claim of parser safety or document coverage. Its numeric limits become usable only with
the pinned adapter's conformance evidence. Changing them changes the profile; runtime settings
may narrow them but cannot silently widen a prepared attempt.

| Resource | Ceiling | Enforcing owner |
| --- | --- | --- |
| Contact chain | initial GET plus at most three redirects; one active passage per admitted binding | Scout reservation and destination gate |
| Transfer | 2 MiB encoded body, 4 MiB decoded body, 20:1 cumulative expansion, 64 KiB headers per response; body budgets aggregate across the chain | bounded transport and decoder |
| End-to-end elapsed time | 30 seconds from prepared admission through extraction, including DNS and redirects | host monotonic deadline |
| Extraction | 5 seconds elapsed, 2 seconds CPU, 256 MiB child memory, within the remaining passage deadline | supervisor and OS limits |
| Parse structure | 50,000 elements, depth 128; no external entity, DTD, XInclude, schema or subresource resolution | pinned parser with construction-time limits |
| Serialized return | 256 KiB UTF-8 including attribution | bounded encoder and IPC |
| Retry, paid spend, frontier | zero | Scout admission and transport |

Expired or revoked authority refuses before each new contact. Slots and worst-case budgets are
reserved atomically against both binding and shared host limits; another binding cannot bypass
the aggregate ceiling. Reservations are not released while owned work may still run. Restart reconciles
uncertain reservations with the retained attempt rather than assuming a timeout refunded them.

Parsing and extraction run in a disposable network-denied child, with no acquired sockets,
credentials or workspace authority. The parent supplies bounded decoded bytes and pinned options;
the child cannot fetch, decompress again, load external resources, or invoke an LLM. The host kills
and reaps owned work on timeout/cancellation. OS CPU and memory limits must cover parsing as well
as extraction; post-parse element counts and coroutine timeouts are insufficient. A parser without
construction-time bounds or observable limit failures is ineligible. Silent recovery truncation,
OOM, child crash, limit failure, or unproved cleanup cannot become successful extraction.

One pinned extractor strategy is selected per profile. Heuristic boilerplate removal records its
declared transformations without claiming an exact semantic-loss percentage. Page titles,
bylines, dates, `<base>` and canonical links remain untrusted claims; they cannot replace the
contacted source or trigger another contact. Wire, decoded-body and output identities remain
distinct. Readiness requires representative ordinary and hostile fixtures before any library or
numerical envelope is qualified.

## Destination and data boundary

Every external acquisition URL, redirect, and new connection is independently authorized: canonical HTTPS
scheme, hostname, port, and origin only; no userinfo or unsupported scheme. Resolution runs
without ambient proxy or credentials and rejects mixed or forbidden addresses (loopback, private,
link-local, multicast, unspecified, and metadata). The approved address is pinned through the
connection; connected peer, TLS certificate, SNI, and `Host` must match. Redirects are manual,
bounded, loop-checked, and re-gated. An HTTP client or crawler SDK alone cannot establish this
SSRF/DNS-rebinding boundary.

An acquisition receipt names the effect, principal/Run, provider/version, policy, locator and
redirects, destination evidence, time, disposition, media/size, usage, and raw digest. It proves
an attempted effect, not a correct response. Responses and derivations remain attributed data:

```text
locator != response != derived statement != trusted fact
acquired != admitted != understood != promoted
```

They are never instructions, code, policy, or truth. Interpretation belongs to
[Drift (34)](34-evaluation.md) or the consuming Composition.

## Custody and stronger tracks

A digest or receipt is not an artifact. Bytes stay ephemeral unless the custody owner verifies
digest, media type, size, classification, retention, retrieval authority, and storage, then
returns a retrievable `ArtifactRef`. Download arrival creates neither a workspace nor an artifact; it enters
bounded quarantine. Render, Interact, Credential Use, Session Custody, Screenshot, and Download
remain Designed. JavaScript or a challenge returns refusal or `human_required`, never a browser.

Any future renderer needs a finite destination/egress grant and isolation from Core peers, host
paths, databases, control sockets, wallets, and unrelated secrets. Credentials are opaque,
principal- and origin-scoped references outside prompts and telemetry. A browser-bearing
Soulstone may not join the shared `lychd.pod`; it needs a dedicated rootless containment and
network zone whose only target egress crosses the Scout gate.

## Qualification evidence

The following are acceptance obligations, not existing tests or runtime receipts. Each profile
must bind its actual finite request, response, parser, time, concurrency and disclosure ceilings;
missing, unlimited, negative or nonfinite values refuse admission. Search additionally fixes
query/locator/title/snippet limits and a per-engine contact plan before qualification.

| Case | Required observation |
| --- | --- |
| Wrong effect, stale binding, forged provenance, mutable registration | refusal before contact; no raw provider tool is exposed |
| Query bangs, preferences, extra parameters, inherited engines, service initialization | no unadmitted recipient or auxiliary request |
| Disconnect, redirect, timeout, quota, challenge, cancellation | no hidden retransmission, provider switch, or stronger effect; uncertain work is not reported settled |
| Partial engine failure, suspension, empty result, missing coverage | distinct attributed observations; no exhaustive-search claim |
| Valid result JSON after a host-denied auxiliary request | terminal refusal, never successful partial output |
| DNS rebinding, mixed public/private answers, peer mismatch, redirect to metadata | no application bytes reach the forbidden destination |
| Compression bomb, malformed framing/JSON, invalid locator, limit and limit-plus-one input | deterministic bounded refusal; no salvaged partial success |
| Deep/wide markup, expensive parsing, OOM, external entities and resources | Core survives; bounded child termination; no unauthorized network or file read |
| Prompt injection, forged receipts, canonical links and provider error text | unchanged authority and host attribution; no follow-up contact, automatic custody or memory promotion |
| Crash between preparation, transmission, extraction and settlement | retained identity and unknown outcome; no blind replay or early budget refund |
| Changed image, parser, engine, effective config or driver | old qualification cannot authorize a new binding |

### Upstream comparisons

These primary references informed the design review on 12 October 2026; they do not qualify a
dependency or deployment. Recheck the exact selected revision before implementation.

- **SearXNG:** [JSON API](https://docs.searxng.org/dev/search_api.html),
  [query syntax](https://docs.searxng.org/user/search-syntax.html) and
  [engine settings](https://docs.searxng.org/admin/settings/settings_engines.html) support the
  separate search adapter, but default-disabled engines and query routing are not authorization.
  At [ad46ebd7d](https://github.com/searxng/searxng/blob/ad46ebd7d/searx/network/network.py#L249-L279),
  the first connection-error branch permits another client request even with zero configured
  retries. This is evidence of a permitted retry path, not proof that every such error retransmits
  application bytes. Qualification must observe and constrain that path.
- **Tavily:** its [Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
  demonstrates optional generated answers, raw content, automatic parameter selection and usage.
  A future private Portal must close those choices explicitly; the endpoint name alone does not
  establish its effects or cost. It remains outside the first-party provider set.
- **Trafilatura and Readability:** [Trafilatura's extraction source](https://trafilatura.readthedocs.io/en/latest/_modules/trafilatura/core.html)
  checks output tree size after parsing/extraction; [Readability](https://github.com/mozilla/readability)
  consumes an already-built DOM and does not sanitize returned HTML. Their article-selection
  mechanisms are comparison candidates, not parser containment. Neither extractor is selected
  without coverage and resource evidence; return plain attributed text through the same boundary.
- **Crawl4AI and Firecrawl:** [Crawl4AI configuration](https://docs.crawl4ai.com/core/browser-crawler-config/)
  exposes browser powers wider than the selected passage; [Firecrawl self-hosting](https://docs.firecrawl.dev/contributing/self-host)
  introduces a broader service stack. Keep the existing renderer candidate and deferred comparison
  until a demonstrated static-coverage gap justifies their operating and containment cost.

## Delivery Boundary

**State: Designed. Scout has no delivered acquisition capability.** There is no Scout built-in,
configuration, provider, route, Agent tool, fetcher, extractor, robots/rate policy, SSRF pinning,
receipt, browser, quarantine, or Reliquary. Installed `httpx` serves other control-plane calls;
`selectolax` is test-only. Generic-runtime “crawler” tests prove only that no capability is
invented. `ArtifactRef` proves metadata, not custody.

The current Extension Context has no Scout-owned provider contribution surface; Animator
capability synthesis still requires a model-shaped identity; `ToolConnector` hydration is
Animator-wide rather than capability/effect-scoped; and current Soulstone compilation joins the
shared Pod. Those are implementation blockers, not permission to approximate the boundary with a
raw provider tool. Source, configuration, adversarial network/parser/browser tests, and an updated
[State of Work](../state-of-the-work.md#scout-web-acquisition) record are required before delivery.
The [Scout topic](../sepulcher/extensions/scout.md) orients the reader.

## Consequences

!!! success "Positive"
    Static reads do not inherit browser, credential, payment, storage, or interpretation authority.
    Redirect, provenance, hostile-content, and custody failures stay visible and recover only by a
    newly admitted effect. Search-engine and browser maintenance stay behind replaceable service
    protocols, while the free local first-party path remains coherent.

!!! failure "Negative"
    Safe acquisition requires more than HTTP and HTML libraries. Dynamic, authenticated, paid, and
    durable material remains unavailable until its separate adversarial boundaries pass. Operators
    choosing paid web providers own a private adapter, its compatibility, and its provider contract.
