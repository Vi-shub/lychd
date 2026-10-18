---
title: Scout source acquisition study
icon: material/rss
---

# Scout: search, feeds and the Bazoš passage

**Maturity: candidate study, reviewed 18 October 2026.** This study selects the next experiment;
it does not deliver a provider, XML extractor, crawler, monitoring service or standing authority.
[Scout](scout.md), [ADR 30](../../adr/30-webcrawler.md) and
[State](../../state-of-the-work.md#scout-web-acquisition) retain those boundaries.

## Choose the source surface before adding a service

| Need | Smallest useful path | Current boundary |
| --- | --- | --- |
| Understand one known public page | Existing static HTTPS Fetch and isolated HTML/text Extract | Opt-in public-page workflow; actual host/profile qualification remains necessary |
| Observe recent Bazoš listings | One advertised category RSS feed, then local extraction/filtering | Feed fetch/extraction profile is the next candidate; XML is currently unsupported |
| Discover sources across the web | SearXNG, one qualified engine and one result page | No admitted engine, Rune or adapter ships; upstream-request mediation remains a prerequisite |
| Read a page whose useful content requires JavaScript | Separately admitted Fetch + Render + Extract | Crawl4AI remains a later isolated candidate, not an automatic fallback |

These paths can develop independently. A Bazoš feed is not a reason to wait for general Search,
and a feed observation is not a claim that Search or Crawl has been implemented.

## What the live source established

On 18 October 2026, one bounded read of the public [PC category](https://pc.bazos.sk/) returned
HTTP 200 and 45,992 bytes of UTF-8 HTML. Its own navigation advertised
[the PC RSS feed](https://www.bazos.sk/rss.php?rub=pc). A separate single read of that exact feed
returned HTTP 200, `text/xml; charset=utf-8`, and 33,385 bytes. These are dated research
observations, not a maintained deployment receipt or a compatibility promise.

The [category robots](https://pc.bazos.sk/robots.txt) and
[main-host robots](https://www.bazos.sk/robots.txt) then disallowed search/filter URL patterns
including `hledat`, location and price parameters. The category root and advertised PC feed
were not excluded by their default rules. Recheck the exact host and locator before an admitted
contact; a linked URL or historical robots observation is not an effect grant. Do not substitute
filtered search URLs, mobile identities, extra pages or another endpoint after refusal.

The existing fixed Scout extractor processed the acquired category HTML in its isolated,
network-denied child and returned 11,461 bytes of text; the child was reaped. Supplying the RSS
response under its actual `text/xml` type correctly refused before child launch. This separates
a useful HTML path from the missing XML path. Recasting XML as HTML, ignoring the media check or
running a second parser inside Core would conceal that gap.

The current HTML strategy deliberately drops attributes, including link targets. Its text
projection can support a bounded page answer; it is not already a structured listing catalogue.

## What other projects contribute

The [Bazoš dataset project](https://github.com/kukas/bazos_dataset) publishes a
[feed-based scraper](https://github.com/kukas/bazos_dataset/blob/main/bazos_scraper.py).
The useful pattern is to read category feeds, retain source identity and observation time, and
compare entries locally. Its historical Czech dataset does not establish current Slovak coverage,
and its repeated polling or retry behavior does not supply LychD authority.

The [Bazoš MCP client](https://github.com/andrijdavid/bazos-mcp/blob/main/src/bazos-client.ts)
uses a different API path with a generated device identifier and User-Agent. This is comparison
material, not the selected acquisition mechanism. No upstream scraper, MCP server, credential
store, retry policy or implementation is incorporated by this study.

The official [SearXNG Search API](https://docs.searxng.org/dev/search_api.html) supports form POST
and explicitly enabled JSON output; public instances can disable it. Its query still reaches
external engines. LychD's selected design therefore needs more than an HTTP JSON wrapper:
one qualified engine, a restricted query dialect, immutable configuration and the exact
pre-transmission transport gate in ADR 30. Do not count installing a stock container as that proof.

## Next vertical slice: one feed, one attributed digest

Start with the advertised PC RSS 2.0 feed and one explicit public purpose. Reuse Scout's durable
attempts, current-authority checks, destination transport and reservations. First establish a
versioned XML media/extraction contract; do not silently widen `static-public-page@1` or
`scout-static-extract@1`. The existing media refusal remains a required negative control.

The candidate must prove:

- One exact host-admitted locator and finite Fetch/Extract ceilings, no pagination, subresource,
  redirect discovery, retry, browser, paid service or publisher contact.
- Parsing in the existing kind of isolated child, with construction-time element/depth limits,
  bounded item/field/output counts and existing hard time/resource ceilings. DTDs, external
  entities, entity expansion, malformed XML, over-limit and truncated input must refuse.
- Plain title/summary plus original item locator, publisher identifier/date as claims, source
  attribution, observation time and declared omissions. No source-authored markup or instructions
  gain authority. Returned links are data; contacting one needs a fresh destination/effect decision.
- Independent ordinary and hostile fixtures sealed before a constructor candidate. Then qualify
  the actual transport/child combination and current native admission path, retaining refusals and
  observed cleanup. A synthetic parser test alone cannot qualify live acquisition.
- Local deduplication and filtering over the admitted observations, with honest coverage. No match
  means no match in that finite response; it never means no matching listing exists on Bazoš.
  The consuming application owns suitability criteria and any proposed consequence.

The first useful result is an attributed digest that can run without a model. Measure it against
that simple baseline before adding model ranking, semantic extraction or a browser. Prices parsed
from source prose remain derived claims; current availability and seller trust are not established.

Repeated watching is a later application policy: explicit purpose, exact feed, cadence, finite
contact/retention budget, revocation and uncertain-attempt reconciliation. Each poll needs current
admission; an idle loop cannot manufacture a new allowance. Retaining prior entries or bodies
needs the ordinary custody owner, not a private cache hidden in an Agent's history.

Altar should expose the source, last observation, coverage, applied local filters, refused/pending
state and the action that requests a new admitted observation. A new fetch and a local re-filter
must be distinguishable. Add that instrument after the first native receipt, so the UI describes
real operation rather than an empty provider catalogue.
