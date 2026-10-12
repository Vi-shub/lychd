"""The single admitted local embedding dialect and its managed command."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from lychd.domain.animation.embeddings import EmbeddingProfile, EmbeddingRefused

if TYPE_CHECKING:
    from lychd.extensions.builtin.animator.soulstones.llamacpp import LlamaCppSoulstoneConfig

PROFILE_ID = "qwen3-06b-q8-b9445@1"
MODEL_ID = "lychd-qwen3-embedding-06b-q8"
IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:39f4f2c5fd4537f85208f905e70269b5b691fbc6de00fd916c37a60a710b7d52"
PROFILE = EmbeddingProfile(
    profile_id=PROFILE_ID,
    model_id=MODEL_ID,
    model_sha256="06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
    image_digest=IMAGE,
    engine_revision="af6528e6df5d798f7f1363ec1141699be0f638e2",
    dialect="llamacpp:b9445:embeddings-float-normalize2",
    conformance_digest="d62f339b644253eaa2bd1bc394376971c5086c7621104acbfb9eac94a92baefa",
    query_prefix="Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:",
    tokenizer="gguf:add_special=true:parse_special=true",
    pooling="last",
    normalization="l2",
    scalar_encoding="float32-le",
    dimension=1024,
    max_input_bytes=4096,
    max_input_tokens=511,
    max_response_bytes=65536,
    timeout_seconds=40,
)


def require_endpoint(base_url: str) -> str:
    """Admit only a credential-free literal loopback HTTP endpoint."""
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise EmbeddingRefused("endpoint") from None  # noqa: EM101 - closed refusal code
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
        or port is None
    ):
        raise EmbeddingRefused("endpoint")  # noqa: EM101 - closed machine-readable refusal code
    return base_url.rstrip("/")


def require_profile_config(stone: LlamaCppSoulstoneConfig) -> None:
    """Refuse mixed authority before runtime hydration or a readiness probe."""
    allowed = {
        "name",
        "runtime",
        "quadlet",
        "embedding_profile",
        "model_path",
        "port",
        "base_url",
        "concurrency",
        "volumes",
    }
    if stone.model_fields_set - allowed:
        raise EmbeddingRefused("profile_overrides")  # noqa: EM101 - closed machine-readable refusal code
    if (
        stone.embedding_profile != PROFILE.profile_id
        or stone.quadlet.image != IMAGE
        or stone.model_path != "/models/embedding.gguf"
    ):
        raise EmbeddingRefused("profile_identity")  # noqa: EM101 - closed machine-readable refusal code
    if stone.base_url is None or stone.port is None:
        raise EmbeddingRefused("endpoint")  # noqa: EM101 - closed refusal code
    endpoint = require_endpoint(str(stone.base_url))
    if urlsplit(endpoint).port != stone.port:
        raise EmbeddingRefused("endpoint")  # noqa: EM101 - closed refusal code


def profile_command(stone: LlamaCppSoulstoneConfig) -> list[str]:
    """Emit the qualified CPU dialect; this is not host resource attestation."""
    require_profile_config(stone)
    if stone.port is None:
        raise EmbeddingRefused("endpoint")  # noqa: EM101 - closed machine-readable refusal code
    return [
        "--sleep-idle-seconds",
        "-1",
        "-m",
        "/models/embedding.gguf",
        "--alias",
        MODEL_ID,
        "--host",
        "127.0.0.1",
        "--port",
        str(stone.port),
        "--pooling",
        "last",
        "--embd-normalize",
        "2",
        "-ngl",
        "0",
        "-t",
        "2",
        "-tb",
        "2",
        "-c",
        "512",
        "-np",
        "1",
        "-b",
        "512",
        "-ub",
        "512",
        "--cache-type-k",
        "f16",
        "--cache-type-v",
        "f16",
        "--cache-ram",
        "0",
        "--fit",
        "off",
        "--flash-attn",
        "off",
        "--embedding",
        "--offline",
        "--no-context-shift",
        "--no-warmup",
        "--no-webui",
    ]
