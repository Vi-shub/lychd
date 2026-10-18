---
title: Scout
icon: material/navigation-variant-outline
---

# :material-navigation-variant-outline: Scout

An Agent proposes one URL. Before contact, the host must decide which acquisition effect,
destination, identity, and budget are admitted. **Scout** carries that exact attempt through
bounded acquisition and attributed return. The response remains external material; the consuming
Composition decides what it means and whether it serves the work.

> _Bring back the voice, and the trail that led you to it._

Scout follows [Webcrawler](../../adr/30-webcrawler.md).
[State](../../state-of-the-work.md#scout-web-acquisition) distinguishes the verified mechanisms
and opt-in public-page workflow from Search, Crawl and Render work that remains unfinished.

## The first passage

The first implementable passage is one static public HTTPS page:

1. **Prepare.** An Agent proposes one exact URL. The Host binds it to the canonical Run, verified
   principal, origin policy, and consent where required. It admits separate Fetch and Extract
   grants with their own ceilings, reserves worst-case budget, and durably records those grants
   in one Scout effect attempt as `prepared` before network I/O.
2. **Pass.** A static adapter authorizes and pins the destination for one bounded GET, repeating
   the gate for every redirect. It uses no ambient proxy, credentials, cookies, subresources,
   JavaScript, or automatic retry. A network-free extractor accepts bounded HTML, XHTML, or plain
   text and returns attributed, fenced material tied to raw and output digests.
3. **Settle.** A second durable transaction records usage and terminal disposition. Raw bytes are
   released after extraction unless a separate custody service admits them.

The passage must resist SSRF and destination rebinding, treat every response as hostile, and
enforce hard network, parser, output, concurrency, and cost ceilings. After a crash, a stranded
bounded-call Scout effect attempt is `unknown_after_crash` unless independent evidence reconciles
it. A missing terminal record never authorizes a blind retry.

Needing JavaScript is a typed static-passage outcome, not permission to invoke Crawl4AI. The
caller must request and receive new Fetch, Render, and Extract effect grants for that exact locator and
provider profile.

## Ten tracks through the wild

Scout separates effects often hidden behind the word “browser.” **Search** discovers locators;
**Fetch** performs one bounded network read; **Extract** transforms acquired bytes without a
network; and **Crawl** manages a finite frontier. **Render** executes hostile site code;
**Interact** clicks, types, submits, or uploads. **Credential Use** presents one scoped secret
reference; **Session Custody** owns cookies and browser state; **Screenshot** requests pixels; and
**Download** transfers a bounded payload into quarantine. Artifact Admission is a separate
custody-owner effect that validates and retains exact bytes. Downloading never implies admission,
and an admitted artifact never becomes trusted or understood merely because it is durable.

Each Scout track needs its own host-owned `ScoutEffectGrant` and budget. A redirect, JavaScript
requirement, CAPTCHA, login, payment challenge, quota response, or failure is a result, never
permission to retry, change provider, present identity, spend, or open a stronger track. Provider
selection cannot widen the effect grant.

## Attempt layers

A bounded synchronous Scout effect uses one Scout-owned effect attempt. An Animator-backed
implementation additionally holds a scoped `CallGrant`; a native host-owned adapter pins its exact
Scout provider/adapter binding and uses no Dispatcher lease. Scout durably records `prepared`
before I/O; `unknown_after_crash` is its domain disposition when no independent terminal evidence
exists. Neither implementation adds a `ServiceJobAttempt@1` for that immediate call. An
asynchronous crawl or render through `JobGrant` keeps its Scout domain job but layers it over Core's
[`ServiceJobAttempt@1`](../../adr/14-workers.md#service-job-attempts): the domain's
`unknown_after_crash` maps to `INDETERMINATE`, and recovery reconciles the same capability-backed
execution binding.

## Contact does not become truth

Following [Oculus](oculus.md), Scout records an attempted acquisition as an **effect receipt**, one
source response as a **bounded observation**, and each transformation as a **derivation** with
parentage and loss. **Interpretation** applies declared criteria and belongs to
[Drift](drift/trials.md) or the consuming Composition. A digest is neither proof nor custody, and
an `ArtifactRef` remains metadata until a service has admitted retrievable bytes under the
[artifact-custody boundary](../../state-of-the-work.md#artifact-reference-contract).

## Sources are senses, not applications

A site, feed, or API is a source surface. Its adapter belongs beneath Scout; the consuming
[Composition](../../compositions/index.md) owns why the observation matters, the criteria applied
to it, and any consequence. Scout may observe a listing. It cannot decide that the listing suits a
person or authorize a purchase.

An admitted application Pattern may therefore place a Scout effect for an exact locator it has
judged eligible for its purpose. That placement does not move Fetch, Download, destination, retry,
or acquisition-receipt semantics into the application. Conversely, Scout's destination grant says
only that contact is allowed; it cannot make the source eligible for an application. Downloaded
bytes enter quarantine, and only the custody owner may validate and return a durable artifact
reference.

A saved Search, Watch, Source Profile, crawl schedule, or deduplicator remains Scout mechanism
until it gains an operator-visible purpose and lifecycle. **Hunter** remains [Shadow's adversarial
Posture](shadow/hunter.md), not a web-acquisition role.

## The chosen road

The first-party path is local and replaceable:

```text
SearXNG Search -> attributed locator
                       |
                       +-> native static Fetch -> offline Extract
                       |
                       +-> newly granted Crawl4AI Fetch + Render + Extract
```

- **SearXNG** is the selected Search Soulstone. It returns locators and snippets; it does not
  contact a result on the Agent's behalf under a Fetch effect grant.
- **Native static Fetch + Extract** is the ordinary passage for a public page that needs no
  JavaScript.
- **Crawl4AI** is the experimental renderer candidate for one exact public HTTPS URL. Despite its
  name, the initial profile receives no Scout Crawl frontier and exposes no arbitrary browser,
  JavaScript, MCP, credentials, session, screenshot, download, or LLM surface.
- **Firecrawl** is deferred, not installed beside Crawl4AI for speculative redundancy. It returns
  to comparison only if it demonstrates a material advantage under the same grants, isolation,
  evidence, and operating budget.
- **Browserless** is not a parallel first-party backend. Raw CDP, Playwright, function execution,
  and download surfaces are wider than the selected renderer passage; it returns only for a proved
  gap, not provider-count symmetry.

There is no first-party paid web-acquisition Portal adapter. A Magus may write a private extension
for Tavily, hosted Firecrawl or Browserless, or another remote provider, accepting its
compatibility, retention, spend, and policy boundary, but it must still enter through Scout. A
Portal declaration or Dispatcher capability grant cannot authorize a query or page contact, and provider
failure never triggers a cloud fallback.

Here **native adapter** means the LychD-owned Rune, probe, narrow request/response translation,
normalization, and conformance tests around a separate service. It does not mean vendoring the
SearXNG engine catalogue or a crawler/browser implementation into Core. The full provider and
mechanism policy belongs to [ADR 30](../../adr/30-webcrawler.md#first-party-mechanism-policy).

For a known source, first compare its existing static page or advertised feed with the cost of
a search service or browser. The [source acquisition study](scout-web-research.md) traces the
Bazoš page/RSS premise and separates the existing HTML passage from the missing XML profile.
It also records the next useful feed digest and its qualification boundary; no listing watcher
ships through that research alone.

The [first Search profile](../../adr/30-webcrawler.md#searxng-search-contract) is deliberately
bounded to one qualified engine and one page. Self-hosting does not keep the query local: the
engine receives an external disclosure. Scout must mediate those requests before transmission,
including any auxiliary contact; installing SearXNG and setting its retry count is insufficient.
An engine failure remains visible alongside any results, and zero results never claims the web
has no answer. No compliant engine profile or transport integration ships yet.

For implementation, start with the [static qualification envelope](../../adr/30-webcrawler.md#initial-qualification-envelope)
and [adversarial acceptance cases](../../adr/30-webcrawler.md#qualification-evidence). The
[native extractor](../../adr/30-webcrawler.md#native-text-extraction-profile) accepts already
acquired UTF-8 HTML or plain text in a network-denied child. It returns an ephemeral text
projection with declared transformations, not browser rendering, article understanding or removal
of source-authored instructions. Its [host qualification procedure](https://github.com/hexanomicon/lychd/blob/main/CONTRIBUTING.md#quality-checks)
uses an explicit runtime profile and synthetic fixtures. Passing it grants no Fetch, custody or
workflow authority. Search and JavaScript rendering retain their separate admission and isolation
gates.

The [native HTTPS transport](../../adr/30-webcrawler.md#native-https-transport-profile) has a
separate isolated-network qualification: real DNS and TLS fixtures lead into the extractor,
while certificate, destination, framing and current-handoff refusals remain visible. It accepts
ordinary HTTP/1.1 HTML/plain-text responses with identity or gzip encoding. Chunk extensions,
trailers and unlisted redirects refuse. This mechanism proof uses a synthetic gate; durable
attempts and current operator authority have their own PostgreSQL and workflow checks. Neither
receipt alone proves a deployed application, a selected model or arbitrary host compatibility.

## Public-page answers

The opt-in `bridge_chat@4` casting prepares a native review, waits for your decision, acquires one
page and answers with one exact local chat capability. Its Bridge command is:

```text
/read-public https://example.com/page
What does this page claim?
```

Sending this command asserts that the complete locator, including its path and query, is public.
The assertion requires the actual authenticated local operator and current Scout classification
policy. It is not proof of publicness. The review identifies the URL, resolver and its declared
custody, DNS/TLS disclosures, resource ceilings, local model and answer retention before contact.
Approval grants neither retries nor additional URLs.

Select revision 4 in `lychd.toml` only with PostgreSQL and an independently qualified extraction
runtime. These example paths and capability names must be replaced with your reviewed inventory:

```toml
[weaver.bridge]
revision = "4"
capability_key = "my-runtime:chat:my-local-model"

[weaver.bridge.scout]
binding_id = "public-page-local"
policy_revision = 1
revocation_generation = 0
resolver_address = "127.0.0.1"
resolver_port = 53
resolver_custody_statement = "This operator-owned resolver forwards the declared public hostname to its configured upstream."
ca_file = "/etc/ssl/certs/ca-certificates.crt"
bwrap = "/usr/bin/bwrap"
python_root = "/opt/scout-python"
libraries = [
  "/lib64/ld-linux-x86-64.so.2", "/lib64/libc.so.6", "/lib64/libdl.so.2",
  "/lib64/libgcc_s.so.1", "/lib64/libm.so.6", "/lib64/libpthread.so.0",
  "/lib64/librt.so.1", "/lib64/libutil.so.1",
]
max_context_chars = 16000
review_ttl_seconds = 300

[weaver.bridge.scout.allowance]
connects = 3
transmissions = 512
wire_bytes = 1048576

[weaver.bridge.scout.budget_limits.host]
connects = 30
transmissions = 5120
wire_bytes = 10485760

[weaver.bridge.scout.budget_limits.binding]
connects = 15
transmissions = 2560
wire_bytes = 5242880
```

The resolver address must be numeric; its reachable TCP listener and upstream custody are operator
configuration. The example does not start a resolver or qualify those paths. The dedicated Python
tree must contain no operator material. Runtime and library qualification follows the maintained
Linux proof procedure in the contribution guide.

The process shares one Scout authority and cancellation fence between HTTP and workers. Each
attempt reserves worst-case outbound counters before I/O; only observed completion settles actual
usage. One binding admits one active acquisition. Budgets are cumulative, and changing a binding
name or restarting cannot reset them. Unknown crash outcomes retain their reservation and slot;
this profile provides no automatic reconciliation or retry. A changed budget configuration refuses
against existing budget rows instead of silently discarding their accounting.

Acquired bytes enter the selected local model as bounded, attributed, untrusted data. Token
counting and inference both require current authority. There are no model tools, remote fallback,
settled-history replay or automatic redirects. The ordinary answer is retained and may quote the
page; raw acquisition and its injected envelope are not saved in Graph state or model history.
Lost transient material cannot be reconstructed from a receipt or silently fetched again.

Revision 4 becomes the selected Bridge profile, so other message forms refuse there. Removing
the selector restores the default for new admissions; retained Runs keep their original revision.
Restart replaces the authentication generation, so prior prepared work cannot acquire fresh
authority merely by restoring its records. Loom displays the registered score and its activation;
it does not edit this casting or activate Search, Crawl or Render.

## The laws of the road

- **Pin before connecting.** Reauthorize every connection and redirect; forbidden or mixed DNS,
  peer mismatch, rebinding, and ambient proxies fail closed.
- **External material remains external.** Queries are classified egress, and returned content may
  contain injection, secrets, falsehood, or hostile structure. It enters Context only as
  attributed, fenced data, never instruction or tool authority.
- **Refusal remains refusal.** Robots policy, site terms, authentication, and law are distinct. A
  denial or challenge returns a typed outcome; Scout does not evade it or rotate identity.
- **Credentials and browsers stay isolated.** Secrets remain opaque and scoped outside prompts
  and ordinary telemetry. Any future renderer is disposable and separated from Core peers, Host
  paths, databases, control sockets, wallets, and unrelated secrets. It does not join the shared
  `lychd.pod`.
- **Bytes enter quarantine before custody.** Arrival grants no workspace, execution, or durable
  artifact name; admission must establish provenance, classification, retention, and retrieval.
- **Spend requires authority.** Reserve bounded resources and spend; paid providers and price
  challenges grant no payment authority.

The full destination checks, receipt fields, parser ceilings, and transition mechanics remain in
[ADR 30](../../adr/30-webcrawler.md).

## The next gate

Scout acquires and attributes. [Prism](prism/index.md) may act on visual bytes only after custody admits
them. [Smith](smith.md) may propose a candidate from admitted sources. None appoints the next:

```text
acquired ≠ admitted ≠ understood ≠ trusted ≠ promoted
```
