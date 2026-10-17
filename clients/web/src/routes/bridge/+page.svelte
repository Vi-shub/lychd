<script lang="ts">
  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import BridgeView from "$lib/components/BridgeView.svelte";
  import CreationInvestigation from "$lib/components/CreationInvestigation.svelte";
  import { investigationHref, investigationSelection } from "$lib/creation/intake";

  const focus = $derived(page.url.searchParams.getAll("investigation"));
  const selected = $derived(focus.length === 1 ? investigationSelection(focus[0] ?? null) : null);
  let navigationError = $state("");
  function selectCase(caseId: string | null) {
    navigationError = "";
    void goto(caseId ? investigationHref(caseId) : "/bridge?investigation=new", {
      replaceState: true, keepFocus: true, noScroll: true
    }).catch(() => { navigationError = "Navigation failed. Keep the displayed case UUID to Check it later."; });
  }
</script>

{#if focus.length === 0}
  <BridgeView />
{:else if selected === null}
  <section aria-labelledby="invalid-investigation">
    <h1 id="invalid-investigation">Investigation selection unavailable</h1>
    <p role="alert">Choose one case UUID or start a new investigation. No request was sent.</p>
    <a href="/bridge?investigation=new">Investigate public source</a>
    <a href="/bridge">Return to conversation</a>
  </section>
{:else}
  {#if navigationError}<p role="alert">{navigationError}</p>{/if}
  <CreationInvestigation caseId={selected === "new" ? null : selected} onSelect={selectCase} />
{/if}
