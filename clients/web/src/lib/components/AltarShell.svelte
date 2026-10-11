<script lang="ts">
  import { page } from "$app/state";
  import { asset, resolve } from "$app/paths";
  import { onMount, setContext } from "svelte";
  import { altarNavigationContext, selectedBridgeHref, type AltarNavigation } from "$lib/navigation/instruments";

  import { getAltarStatus } from "$lib/api/client";

  let { children } = $props();
  let pending = $state<number | null>(null);
  let omens = $state<Array<{ id: number; text: string; fault: boolean }>>([]);
  const navigation = $state<AltarNavigation>({ bridgePath: "/bridge" });
  setContext(altarNavigationContext, navigation);

  // Retain the explicit session across instrument lifetimes.
  $effect(() => {
    const selectedSession = selectedBridgeHref(page.url.pathname, resolve("/bridge"));
    if (selectedSession) navigation.bridgePath = selectedSession;
  });

  let nextOmen = 0;
  let attentionVersion = 0;
  let attentionInFlight = false;
  let attentionQueued = false;
  let attentionTimer: number | undefined;
  let attentionFault = "";
  let mounted = false;

  function manageAboutDisclosure(node: HTMLDetailsElement) {
    function dismissOutside(event: MouseEvent) {
      if (node.open && !event.composedPath().includes(node)) node.open = false;
    }

    function dismissOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape" || event.defaultPrevented || !node.open) return;
      node.open = false;
      node.querySelector("summary")?.focus();
      event.preventDefault();
    }

    window.addEventListener("click", dismissOutside);
    window.addEventListener("keydown", dismissOnEscape);
    return () => {
      window.removeEventListener("click", dismissOutside);
      window.removeEventListener("keydown", dismissOnEscape);
    };
  }

  const instruments = [
    { slug: "atlas", label: "Atlas" },
    { slug: "bridge", label: "Bridge" },
    { slug: "orb", label: "Orb" },
    { slug: "nexus", label: "Nexus" },
    { slug: "loom", label: "Loom" }
  ] as const;

  function attention(_event: Event) {
    // Instrument events are invalidation hints; only the shell status endpoint
    // owns the cross-session count.
    void refreshAttention();
  }

  function receiveOmen(event: Event) {
    const detail = (event as CustomEvent<{ text: string; fault?: boolean }>).detail;
    raiseOmen(detail.text, detail.fault ?? true);
  }

  function raiseOmen(text: string, fault = true) {
    const id = ++nextOmen;
    omens.push({ id, text, fault });
    if (!fault) window.setTimeout(() => dismiss(id), 6000);
  }

  function dismiss(id: number) {
    omens = omens.filter((omen) => omen.id !== id);
  }

  function clearAttentionTimer() {
    if (attentionTimer !== undefined) window.clearTimeout(attentionTimer);
    attentionTimer = undefined;
  }

  function refreshAttention() {
    if (!mounted || document.visibilityState !== "visible") return;
    clearAttentionTimer();
    pending = null;
    const version = ++attentionVersion;
    if (attentionInFlight) {
      attentionQueued = true;
      return;
    }
    attentionInFlight = true;
    void readAttention(version);
  }

  async function readAttention(version: number) {
    try {
      const next = (await getAltarStatus()).pending_consents;
      if (mounted && version === attentionVersion) {
        pending = next;
        attentionFault = "";
      }
    } catch (error) {
      if (!mounted || version !== attentionVersion) return;
      pending = null;
      const message = error instanceof Error ? error.message : "The Altar cannot be reached.";
      if (message !== attentionFault) raiseOmen(message);
      attentionFault = message;
    } finally {
      attentionInFlight = false;
      if (mounted && document.visibilityState === "visible") {
        if (attentionQueued) {
          attentionQueued = false;
          refreshAttention();
        } else attentionTimer = window.setTimeout(refreshAttention, 5000);
      }
    }
  }

  function attentionVisibilityChanged() {
    if (document.visibilityState === "visible") refreshAttention();
    else {
      attentionVersion++;
      attentionQueued = false;
      pending = null;
      clearAttentionTimer();
    }
  }

  onMount(() => {
    mounted = true;
    void refreshAttention();
    window.addEventListener("altar:attention", attention);
    window.addEventListener("altar:omen", receiveOmen);
    return () => {
      mounted = false;
      attentionVersion++;
      attentionQueued = false;
      clearAttentionTimer();
      window.removeEventListener("altar:attention", attention);
      window.removeEventListener("altar:omen", receiveOmen);
    };
  });
</script>

<svelte:window onfocus={refreshAttention} />
<svelte:document onvisibilitychange={attentionVisibilityChanged} />

<a class="skip-link" href="#altar-main">Skip to instrument</a>
<header class="topbar">
  <details class="altar-about" {@attach manageAboutDisclosure}>
    <summary class="brand" aria-label="About LychD — The Altar">
      <img class="mark" src={asset("/favicon.svg")} width="32" height="32" alt="" />
      <span class="brand-wordmark">
        <span class="name rune-head">LychD</span>
        <span class="sub">The Altar <span class="about-caret" aria-hidden="true">⌄</span></span>
      </span>
    </summary>
    <div class="altar-about__panel">
      <h2>About the Altar</h2>
      <p class="altar-about__version" title={__LYCHD_ALTAR_VERSION__}>
        Altar {__LYCHD_ALTAR_VERSION__.slice(0, 12)}
      </p>
      <div class="altar-about__links">
        <a href={__LYCHD_SOURCE_URL__} rel="noreferrer">{__LYCHD_SOURCE_LABEL__}</a>
      </div>
    </div>
  </details>

  <nav class="instruments" aria-label="Instruments">
    {#each instruments as instrument (instrument.slug)}
      <a
        href={resolve(instrument.slug === "bridge" ? navigation.bridgePath : `/${instrument.slug}`)}
        aria-current={page.url.pathname.startsWith(`/${instrument.slug}`) ? "page" : undefined}
      >
        <span>{instrument.label}</span>
      </a>
    {/each}
  </nav>

  <div class="spacer"></div>
  <a
    class="sigil"
    data-state={pending === null ? "unknown" : pending > 0 ? "lit" : "dormant"}
    href={resolve("/bridge?attention=pending")}
  >
    ⬡ {pending === null ? "Consent status unknown" : pending > 0 ? `${pending} awaiting` : "Consent clear"}
  </a>
  <span class="sigil-identity" title="Fixed local authority context; not authentication">
    Local Sigil · <b>Magus</b>
  </span>
</header>

<main id="altar-main">
  {@render children()}
</main>

<div class="omen-stack" role="status" aria-live="polite">
  {#each omens as omen (omen.id)}
    <div class="omen" data-state={omen.fault ? "fault" : "info"}>
      <span>{omen.text}</span>
      <button class="dismiss" type="button" onclick={() => dismiss(omen.id)} aria-label="Dismiss">✕</button>
    </div>
  {/each}
</div>
