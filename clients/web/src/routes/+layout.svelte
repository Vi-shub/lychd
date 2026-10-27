<script lang="ts">
  import "../app.css";
  import { setContext } from "svelte";
  import { page } from "$app/state";
  import { bridgeWorkspaceKey, createBridgeWorkspace } from "$lib/bridge/workspace.svelte";
  import AltarShell from "$lib/components/AltarShell.svelte";

  let { children } = $props();
  const bridgeWorkspace = setContext(bridgeWorkspaceKey, createBridgeWorkspace());
  function guardUnload(event: BeforeUnloadEvent) {
    if (!bridgeWorkspace.needsUnloadWarning) return;
    event.preventDefault();
    event.returnValue = "";
  }
</script>

<svelte:window onbeforeunload={guardUnload} />

{#if import.meta.env.DEV && page.url.pathname === "/canvas"}
  {@render children()}
{:else}
  <AltarShell>
  {@render children()}
  </AltarShell>
{/if}
