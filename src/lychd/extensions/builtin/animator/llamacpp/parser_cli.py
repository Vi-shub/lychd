from __future__ import annotations

from typing import Final

from lychd.extensions.builtin.animator.llamacpp.parser_models import LlamaCppRuntimeInference


class LlamaCppCliInferenceParser:
    """Infer llama.cpp runtime metadata from CLI/env inputs."""

    _NO_VALUE_FLAGS: Final[set[str]] = {
        "--models-autoload",
        "--no-models-autoload",
        "--jinja",
        "--no-jinja",
        "--spec-default",
    }
    _SHORT_TO_LONG: Final[dict[str, str]] = {
        "-m": "--model",
        "-mu": "--model-url",
        "-dr": "--docker-repo",
        "-hf": "--hf-repo",
        "-hfr": "--hf-repo",
        "-hff": "--hf-file",
        "-hfd": "--hf-repo-draft",
        "-a": "--alias",
        "-c": "--ctx-size",
        "-n": "--n-predict",
        "-np": "--parallel",
    }
    _LONG_TO_CANONICAL: Final[dict[str, str]] = {
        "--predict": "--n-predict",
    }
    _SOURCE_FLAGS: Final[set[str]] = {
        "--alias",
        "--model",
        "--model-url",
        "--docker-repo",
        "--hf-repo",
        "--hf-file",
    }
    _ROUTER_FLAGS: Final[set[str]] = {
        "--models-dir",
        "--models-preset",
    }

    # Only metadata inputs, neutral draft controls, and arguments emitted by our
    # managed planner have known arity here. Unknown options never establish the
    # role of a later source token.
    _VALUE_FLAGS: Final[set[str]] = {
        "--alias",
        "--port",
        "--ctx-size",
        "--parallel",
        "--kv-unified-per-slot",
        "--n-predict",
        "--temp",
        "--top-p",
        "--host",
        "--sleep-idle-seconds",
        "--models-max",
        "--chat-template",
        "--lora",
        "--n-cpu-moe",
        "-ngl",
        "-ctk",
        "-ctv",
        "-sm",
        "-fa",
        "-t",
        "-tb",
        "--hf-repo-draft",
        "--model-draft",
        "-md",
    }

    def infer_args(self, args: list[str]) -> LlamaCppRuntimeInference:
        """Infer runtime metadata from explicit CLI args."""
        if not args:
            return LlamaCppRuntimeInference()

        options = self._parse_cli_options(args)
        return self._resolve_sources(
            LlamaCppRuntimeInference(
                model_aliases=self._parse_aliases(options.get("--alias")),
                hf_repo=self._as_str(options.get("--hf-repo")),
                docker_repo=self._as_str(options.get("--docker-repo")),
                model_path=self._as_str(options.get("--model")),
                models_dir=self._as_str(options.get("--models-dir")),
                models_preset=self._as_str(options.get("--models-preset")),
                port=self._as_present_int(options.get("--port")),
                n_ctx=self._as_present_int(options.get("--ctx-size")),
                n_parallel=self._as_present_int(options.get("--parallel")),
                n_ctx_per_slot=self._as_present_int(options.get("--kv-unified-per-slot")),
                n_predict=self._as_int(options.get("--n-predict")),
                temperature=self._as_float(options.get("--temp")),
                top_p=self._as_float(options.get("--top-p")),
            )
        )

    def infer_env(self, env: dict[str, str]) -> LlamaCppRuntimeInference:
        """Infer runtime metadata from LLAMA_ARG_* environment variables."""
        if not env:
            return LlamaCppRuntimeInference()

        return self._resolve_sources(
            LlamaCppRuntimeInference(
                model_aliases=self._parse_aliases(env.get("LLAMA_ARG_ALIAS")),
                hf_repo=env.get("LLAMA_ARG_HF_REPO"),
                docker_repo=env.get("LLAMA_ARG_DOCKER_REPO"),
                model_path=env.get("LLAMA_ARG_MODEL"),
                models_dir=env.get("LLAMA_ARG_MODELS_DIR"),
                models_preset=env.get("LLAMA_ARG_MODELS_PRESET"),
                port=self._as_present_int(env.get("LLAMA_ARG_PORT")),
                n_ctx=self._as_present_int(env.get("LLAMA_ARG_CTX_SIZE")),
                n_parallel=self._as_present_int(env.get("LLAMA_ARG_N_PARALLEL")),
                n_ctx_per_slot=self._as_present_int(env.get("LLAMA_ARG_KV_UNIFIED_PER_SLOT")),
                n_predict=self._as_int(env.get("LLAMA_ARG_N_PREDICT")),
                temperature=self._as_float(env.get("LLAMA_ARG_TEMPERATURE")),
                top_p=self._as_float(env.get("LLAMA_ARG_TOP_P")),
            )
        )

    def merge(
        self,
        *,
        primary: LlamaCppRuntimeInference,
        secondary: LlamaCppRuntimeInference,
    ) -> LlamaCppRuntimeInference:
        """Overlay source options, union additive aliases, then derive mode and identity."""
        return self._resolve_sources(
            LlamaCppRuntimeInference(
                model_aliases=primary.model_aliases | secondary.model_aliases,
                hf_repo=primary.hf_repo if primary.hf_repo is not None else secondary.hf_repo,
                docker_repo=primary.docker_repo if primary.docker_repo is not None else secondary.docker_repo,
                model_path=primary.model_path if primary.model_path is not None else secondary.model_path,
                models_dir=primary.models_dir if primary.models_dir is not None else secondary.models_dir,
                models_preset=primary.models_preset if primary.models_preset is not None else secondary.models_preset,
                port=primary.port if primary.port is not None else secondary.port,
                n_ctx=primary.n_ctx if primary.n_ctx is not None else secondary.n_ctx,
                n_parallel=primary.n_parallel if primary.n_parallel is not None else secondary.n_parallel,
                n_ctx_per_slot=primary.n_ctx_per_slot
                if primary.n_ctx_per_slot is not None
                else secondary.n_ctx_per_slot,
                n_predict=primary.n_predict if primary.n_predict is not None else secondary.n_predict,
                temperature=primary.temperature if primary.temperature is not None else secondary.temperature,
                top_p=primary.top_p if primary.top_p is not None else secondary.top_p,
            )
        )

    def _canonical_option(self, token: str) -> str:
        key = token.partition("=")[0].replace("_", "-")
        return self._SHORT_TO_LONG.get(key, self._LONG_TO_CANONICAL.get(key, key))

    def _parse_cli_options(self, args: list[str]) -> dict[str, str | bool]:
        options: dict[str, str | bool] = {}
        source_flags = self._SOURCE_FLAGS | self._ROUTER_FLAGS
        unknown_arity = False
        index = 0
        while index < len(args):
            token = args[index]
            if not token.startswith("-"):
                index += 1
                continue
            key = self._canonical_option(token)
            if key in source_flags and (unknown_arity or "=" in token):
                msg = "The llama.cpp source option requires an unambiguous positional value before unknown options."
                raise ValueError(msg)
            if "=" in token:
                self._record_cli_value(options, key, token.partition("=")[2])
                unknown_arity |= key not in self._VALUE_FLAGS and key not in self._NO_VALUE_FLAGS
                index += 1
                continue
            if key in self._NO_VALUE_FLAGS:
                options[key] = True
                index += 1
                continue
            if key not in self._VALUE_FLAGS and key not in source_flags:
                unknown_arity = True
            value = args[index + 1] if index + 1 < len(args) else None
            if value is not None and self._canonical_option(value) in source_flags:
                msg = "The llama.cpp source option cannot also be another option's value."
                raise ValueError(msg)
            if key in source_flags and (value is None or value.startswith("-")):
                msg = "The llama.cpp source option requires a separate value that does not start with '-'."
                raise ValueError(msg)
            # Known value options consume negative numeric inputs too. Invalid
            # numeric metadata remains present and suppresses lower-priority input.
            if value is not None and (not value.startswith("-") or key in self._VALUE_FLAGS):
                self._record_cli_value(options, key, value)
                index += 2
                continue
            options[key] = True
            index += 1
        return options

    def _record_cli_value(self, options: dict[str, str | bool], key: str, value: str) -> None:
        previous = options.get(key)
        if key == "--alias" and isinstance(previous, str):
            value = f"{previous},{value}"
        options[key] = value

    def _parse_aliases(self, value: object) -> frozenset[str]:
        if not isinstance(value, str):
            return frozenset()
        # The supported profile trims C-locale ASCII whitespace. Do not apply
        # Python's broader Unicode whitespace normalization to provider ids.
        return frozenset(alias for item in value.split(",") if (alias := item.strip(" \t\n\r\v\f")))

    def _resolve_sources(self, inferred: LlamaCppRuntimeInference) -> LlamaCppRuntimeInference:
        """Classify main selectors, independently of router hints and neutral inputs."""
        if inferred.model_path or inferred.docker_repo or inferred.hf_repo:
            inferred.mode = "single"
        elif inferred.models_dir is not None or inferred.models_preset is not None:
            inferred.mode = "router"
        inferred.model_provider = (
            min(inferred.model_aliases, default=None) or inferred.hf_repo or inferred.docker_repo or inferred.model_path
        )
        return inferred

    def _as_str(self, value: object) -> str | None:
        if isinstance(value, str):
            return value
        return None

    def _as_int(self, value: object) -> int | None:
        if not isinstance(value, str):
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _as_present_int(self, value: object) -> int | None:
        """Keep present invalid/automatic integer inputs from falling back to a lower source."""
        if value is None:
            return None
        return self._as_int(value) or 0

    def _as_float(self, value: object) -> float | None:
        if not isinstance(value, str):
            return None
        try:
            return float(value)
        except ValueError:
            return None


__all__ = ["LlamaCppCliInferenceParser"]
