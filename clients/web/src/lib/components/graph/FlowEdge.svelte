<script lang="ts">
  import { BaseEdge, getBezierPath, type EdgeProps } from "@xyflow/svelte";
  let { id, source, target, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, label, data }: EdgeProps = $props();
  let loop = $derived(source === target);
  let backwards = $derived(!loop && targetX <= sourceX);
  let geometry = $derived.by(() => {
    if (loop) return [`M ${sourceX} ${sourceY} C ${sourceX + 40} ${sourceY - 95}, ${targetX - 40} ${targetY - 95}, ${targetX} ${targetY}`, (sourceX + targetX) / 2, sourceY - 72] as const;
    if (backwards) {
      const top = Math.min(sourceY, targetY) - 130;
      return [`M ${sourceX} ${sourceY} C ${sourceX + 95} ${top}, ${targetX - 95} ${top}, ${targetX} ${targetY}`, (sourceX + targetX) / 2, top + 25] as const;
    }
    return getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  });
</script>

<BaseEdge {id} path={geometry[0]} labelX={geometry[1]} labelY={geometry[2]}
  label={loop ? `${label ?? ""} · return` : label} {markerEnd}
  style={`stroke:var(--color-rune-dim);stroke-width:1.6;${data?.relation === "order" ? "stroke-dasharray:5 5;" : ""}`}
  labelStyle="fill:var(--color-parchment);font-size:11px" />
