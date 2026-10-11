---
title: vLLM
icon: material/rocket-launch
---

# :material-rocket-launch: vLLM

**vLLM** is a resident model-serving engine behind an OpenAI-compatible connector. The Soulstone
adapter binds one declared model service to a Rune, derives its model capability, probes the live
inventory, and exposes it only when the exact declared model is ready.

A server pinned to one model produces `is_dynamic=False` capabilities. Reachability establishes
link liveness; a capability becomes `WARM` only when the validated live `/models` inventory also
contains its exact declared model id. Malformed inventory or a missing id becomes `ERROR`.
Focused tests cover current planning and connector behavior.

vLLM framework arguments come from the operator-owned `exec` list, which the adapter passes
through verbatim. Image, devices, mounts, ports, and environment remain typed Soulstone and
Quadlet fields. Keep the command's served model id and endpoint consistent with the Rune;
these declarations do not synthesize vLLM flags.

The exact model, quantization, GPU topology, image digest, arguments, readiness, inference, and
shutdown still require an operator receipt. The current delivery boundary is recorded under
[vLLM integration](../../../../state-of-the-work.md#vllm-integration).

See [Soulstone Disciplines](../disciplines.md#i-the-kinetic-vllm), [vLLM](https://docs.vllm.ai/),
and the [OpenAI-compatible connector boundary](../../connectors.md#openai-compatibility-is-per-dialect).
