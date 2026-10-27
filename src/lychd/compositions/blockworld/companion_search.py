"""Inactive native catalogue Dump & Search; proposals never grant game authority.

The injected source supplies a bounded, read-only dump. No current Minecraft
bridge adapter, executable selection, model call or foreign-agent adapter is
provided here. A supplied ranking is untrusted IDs-only presentation advice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_graph import BaseNode, End, GraphRunContext

from lychd.agents.router import Intent
from lychd.agents.services import WorkflowServices
from lychd.agents.workflows.base import PatternEdge, PatternManifest, PatternNode, Trigger, Workflow
from lychd.compositions.blockworld.companion import canonical_uuid, strict_json
from lychd.domain.cortex.graph import build_serial_graph

MAX_DUMP_BYTES = 131_072
MAX_DUMP_ITEMS = 256
MAX_CANDIDATES = 16
MAX_CHOICES = 3
MAX_HOST_ID = 100
MAX_DUMP_TIMEOUT_S = 5
SEARCH_SOURCE = "blockworld.companion.search"
SEARCH_SCOPE = "blockworld:catalogue"
Identity = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-z0-9][a-z0-9._:-]*$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
RegistryId = Annotated[str, Field(max_length=160, pattern=r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")]


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _plain(value: str) -> str:
    if value != value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
        msg = "Catalogue text requires plain printable content without outer whitespace."
        raise ValueError(msg)
    return value


class SearchDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class CatalogueBinding(SearchDocument):
    """Exact source closure selected by the host, never an effect capability."""

    source_id: Identity
    revision: Identity
    digest: Digest


class CatalogueItem(SearchDocument):
    """Source-authored discovery facts; no commands, NBT, templates or execution payload."""

    id: RegistryId
    label: str = Field(min_length=1, max_length=64)
    characteristics: tuple[Annotated[str, Field(min_length=1, max_length=96)], ...] = Field(min_length=1, max_length=4)
    terms: tuple[Annotated[str, Field(min_length=1, max_length=48)], ...] = Field(default=(), max_length=16)

    _label = field_validator("label")(_plain)

    @field_validator("characteristics", "terms", mode="before")
    @classmethod
    def plain_sequence(cls, value: Any) -> Any:
        if type(value) is list:
            value = tuple(cast("list[Any]", value))
        if type(value) is tuple:
            for part in cast("tuple[object, ...]", value):
                if isinstance(part, str):
                    _plain(part)
        return cast("Any", value)


class CatalogueSnapshot(SearchDocument):
    """Finite dump with explicit completeness; digest covers all returned facts."""

    source_id: Identity
    revision: Identity
    items: tuple[CatalogueItem, ...] = Field(max_length=MAX_DUMP_ITEMS)
    total_count: int = Field(ge=0, le=1_000_000)
    truncated: bool

    @field_validator("items", mode="before")
    @classmethod
    def item_sequence(cls, value: Any) -> Any:
        return tuple(cast("list[Any]", value)) if type(value) is list else value

    @model_validator(mode="after")
    def complete_and_bounded(self) -> CatalogueSnapshot:
        if self.total_count < len(self.items) or self.truncated != (self.total_count > len(self.items)):
            msg = "Catalogue total and truncation must describe the returned dump exactly."
            raise ValueError(msg)
        if len({item.id for item in self.items}) != len(self.items):
            msg = "Catalogue item identities must be unique."
            raise ValueError(msg)
        if len(self.model_dump_json().encode()) > MAX_DUMP_BYTES:
            msg = "Catalogue dump exceeds its byte bound."
            raise ValueError(msg)
        return self

    @property
    def binding(self) -> CatalogueBinding:
        return CatalogueBinding(
            source_id=self.source_id, revision=self.revision, digest=_digest(self.model_dump(mode="json"))
        )


def decode_catalogue(document: bytes) -> CatalogueSnapshot:
    """Reject oversized/duplicate/nonfinite input before retaining any dump."""
    return CatalogueSnapshot.model_validate(strict_json(document, limit=MAX_DUMP_BYTES))


class RerankProposal(SearchDocument):
    """Optional source-bound ordered subset of candidate IDs, never actions."""

    binding: CatalogueBinding
    candidate_digest: Digest
    ids: tuple[RegistryId, ...] = Field(max_length=MAX_CANDIDATES)

    @field_validator("ids", mode="before")
    @classmethod
    def id_sequence(cls, value: Any) -> Any:
        return tuple(cast("list[Any]", value)) if type(value) is list else value

    @model_validator(mode="after")
    def unique_ids(self) -> RerankProposal:
        if len(set(self.ids)) != len(self.ids):
            msg = "Ranking cannot repeat candidate IDs."
            raise ValueError(msg)
        return self


class SearchRequest(SearchDocument):
    request_id: str
    binding: CatalogueBinding
    query: str = Field(min_length=1, max_length=128)
    language: Literal["sk", "cs", "en"] = "en"
    proposal: RerankProposal | None = None
    _request_id = field_validator("request_id")(canonical_uuid)
    _query = field_validator("query")(_plain)

    @model_validator(mode="after")
    def source_bound_proposal(self) -> SearchRequest:
        if self.proposal is not None and self.proposal.binding != self.binding:
            msg = "Ranking proposal must name the admitted source closure."
            raise ValueError(msg)
        return self


def decode_search_request(document: str) -> SearchRequest:
    return SearchRequest.model_validate(strict_json(document))


class SearchResult(SearchDocument):
    """Matches are counted only in the returned dump, never in an unseen remainder."""

    request_id: str
    binding: CatalogueBinding
    query: str
    match_count: int = Field(ge=0, le=MAX_DUMP_ITEMS)
    dump_count: int = Field(ge=0, le=MAX_DUMP_ITEMS)
    catalogue_total: int = Field(ge=0, le=1_000_000)
    catalogue_truncated: bool
    candidates_truncated: bool
    candidates: tuple[CatalogueItem, ...] = Field(max_length=MAX_CANDIDATES)
    choices: tuple[CatalogueItem, ...] = Field(max_length=MAX_CHOICES)
    spoken_choices: tuple[str, ...] = Field(max_length=MAX_CHOICES)
    candidate_digest: Digest
    ranking: Literal["literal", "supplied_proposal"]

    @field_validator("candidates", "choices", "spoken_choices", mode="before")
    @classmethod
    def result_sequences(cls, value: Any) -> Any:
        return tuple(cast("list[Any]", value)) if type(value) is list else value


def _fold(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(char))


def literal_search(snapshot: CatalogueSnapshot, request: SearchRequest) -> SearchResult:
    """Use escaped literal alternatives only; caller text never becomes regex syntax."""
    snapshot = CatalogueSnapshot.model_validate(snapshot.model_dump(mode="python", warnings=False))
    request = SearchRequest.model_validate(request.model_dump(mode="python", warnings=False))
    if snapshot.binding != request.binding:
        msg = "Dump differs from the exact admitted source closure."
        raise ValueError(msg)
    query = _fold(request.query)
    if not query.strip():
        msg = "Search query has no literal searchable content."
        raise ValueError(msg)
    aliases = ("castle", "hrad", "zamek") if query in {"castle", "castles", "hrad", "hrady", "zamek"} else (query,)
    pattern = re.compile("|".join(re.escape(alias) for alias in aliases))
    matches = sorted(
        (
            item
            for item in snapshot.items
            if pattern.search(_fold(" ".join((item.id, item.label, *item.characteristics, *item.terms))))
        ),
        key=lambda item: item.id,
    )
    candidates = tuple(matches[:MAX_CANDIDATES])
    candidate_digest = _digest(
        {
            "binding": request.binding.model_dump(mode="json"),
            "query": request.query,
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
    )
    ranking: Literal["literal", "supplied_proposal"] = "literal"
    proposal = request.proposal
    if proposal is not None:
        by_id = {item.id: item for item in candidates}
        if (
            proposal.binding != request.binding
            or proposal.candidate_digest != candidate_digest
            or any(identity not in by_id for identity in proposal.ids)
        ):
            msg = "Ranking proposal is not bound to these exact candidates."
            raise ValueError(msg)
        candidates = tuple(by_id[identity] for identity in proposal.ids) + tuple(
            item for item in candidates if item.id not in proposal.ids
        )
        ranking = "supplied_proposal"
    choices = candidates[:MAX_CHOICES]
    prefixes = {"en": "Choice", "sk": "Možnosť", "cs": "Možnost"}
    spoken = tuple(
        f"{prefixes[request.language]} {index}: {item.label}. {item.characteristics[0]}."
        for index, item in enumerate(choices, start=1)
    )
    return SearchResult(
        request_id=request.request_id,
        binding=request.binding,
        query=request.query,
        match_count=len(matches),
        dump_count=len(snapshot.items),
        catalogue_total=snapshot.total_count,
        catalogue_truncated=snapshot.truncated,
        candidates_truncated=len(matches) > MAX_CANDIDATES,
        candidates=candidates,
        choices=choices,
        spoken_choices=spoken,
        candidate_digest=candidate_digest,
        ranking=ranking,
    )


class CatalogueSource(Protocol):
    """Host-injected read-only adapter; returned UTF-8 JSON must fit MAX_DUMP_BYTES."""

    async def dump(self) -> bytes: ...


class DumpSearchState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    session_id: str
    prompt: str
    manifest_digest: str
    request: SearchRequest
    snapshot: CatalogueSnapshot | None = None
    result: SearchResult | None = None


@dataclass(frozen=True)
class DumpSearchDeps:
    common: WorkflowServices
    source: CatalogueSource
    binding: CatalogueBinding
    session_id: str
    principal: str
    manifest_digest: str
    dump_timeout_s: float


def _check_state(state: DumpSearchState, deps: DumpSearchDeps) -> None:
    """Validate continuation facts before a read; reconstruct every derived result."""
    checked = DumpSearchState.model_validate(state.model_dump(mode="python", warnings=False))
    admitted = decode_search_request(checked.prompt)
    if (
        checked.session_id != deps.session_id
        or checked.manifest_digest != deps.manifest_digest
        or checked.request != admitted
        or admitted.binding != deps.binding
    ):
        msg = "Search checkpoint differs from its admitted input or source."
        raise ValueError(msg)
    canonical_uuid(checked.run_id)
    _check_authority(deps)
    if checked.snapshot is not None and checked.snapshot.binding != deps.binding:
        msg = "Retained catalogue differs from the source closure."
        raise ValueError(msg)
    if checked.result is not None and (
        checked.snapshot is None or checked.result != literal_search(checked.snapshot, admitted)
    ):
        msg = "Retained search result differs from the exact dump and query."
        raise ValueError(msg)


def _check_authority(deps: DumpSearchDeps) -> None:
    principal = deps.common.sigil_provider()
    if principal.name != deps.principal or SEARCH_SCOPE not in principal.scopes:
        msg = "Principal has no catalogue read authority."
        raise ValueError(msg)


@dataclass
class Dump(BaseNode[DumpSearchState, DumpSearchDeps]):
    async def run(self, ctx: GraphRunContext[DumpSearchState, DumpSearchDeps]) -> LiteralSearch:
        _check_state(ctx.state, ctx.deps)
        if ctx.state.snapshot is None:
            async with asyncio.timeout(ctx.deps.dump_timeout_s):
                document = await ctx.deps.source.dump()
            _check_authority(ctx.deps)
            if type(document) is not bytes:
                msg = "Catalogue adapter must return bounded UTF-8 JSON bytes."
                raise ValueError(msg)
            snapshot = decode_catalogue(document)
            if snapshot.binding != ctx.deps.binding:
                msg = "Catalogue adapter returned a changed source closure."
                raise ValueError(msg)
            ctx.state.snapshot = snapshot
        return LiteralSearch()


@dataclass
class LiteralSearch(BaseNode[DumpSearchState, DumpSearchDeps]):
    async def run(self, ctx: GraphRunContext[DumpSearchState, DumpSearchDeps]) -> Report:
        _check_state(ctx.state, ctx.deps)
        if ctx.state.snapshot is None:
            msg = "Search requires a retained validated dump."
            raise ValueError(msg)
        ctx.state.result = literal_search(ctx.state.snapshot, ctx.state.request)
        return Report()


@dataclass
class Report(BaseNode[DumpSearchState, DumpSearchDeps, SearchResult]):
    async def run(self, ctx: GraphRunContext[DumpSearchState, DumpSearchDeps]) -> End[SearchResult]:
        _check_state(ctx.state, ctx.deps)
        if ctx.state.result is None:
            msg = "Search has no retained result."
            raise ValueError(msg)
        ctx.deps.common.events.emitter(ctx.state.run_id).log(ctx.state.result.model_dump_json())
        return End(ctx.state.result)


def dump_search_workflow(
    source: CatalogueSource, *, binding: CatalogueBinding, session_id: str, principal: str, dump_timeout_s: float = 3.0
) -> Workflow:
    """Build an explicitly injected native Pattern; never registers it at normal boot.

    The caller selects the source closure and host authority. This factory neither
    discovers a game adapter nor enables structure placement or summoning.
    """
    binding = CatalogueBinding.model_validate(binding.model_dump(mode="python", warnings=False))
    if (
        not 1 <= len(session_id) <= MAX_HOST_ID
        or not 1 <= len(principal) <= MAX_HOST_ID
        or type(dump_timeout_s) not in (int, float)
        or not 0 < dump_timeout_s <= MAX_DUMP_TIMEOUT_S
    ):
        msg = "Search needs explicit host identity and a finite read timeout."
        raise ValueError(msg)
    manifest = PatternManifest(
        key=SEARCH_SOURCE,
        revision="1",
        implementation_revision="py.1",
        checkpoint_schema="companion-search-v1",
        entry_node="dump",
        nodes=(
            PatternNode(key="dump", label="Dump finite catalogue", implementation=Dump),
            PatternNode(key="literal-search", label="Search literal candidates", implementation=LiteralSearch),
            PatternNode(key="report", label="Report bounded choices", implementation=Report),
            PatternNode(key="end", label="End", kind="terminal"),
        ),
        edges=(
            PatternEdge(key="dump-search", source="dump", target="literal-search"),
            PatternEdge(key="search-report", source="literal-search", target="report"),
            PatternEdge(key="report-end", source="report", target="end"),
        ),
    )

    def make_state(intent: Intent) -> DumpSearchState:
        request = decode_search_request(intent.prompt)
        if (
            intent.source != SEARCH_SOURCE
            or intent.session_id != session_id
            or intent.run_id is None
            or intent.sigil_name != principal
            or SEARCH_SCOPE not in intent.sigil_scopes
            or request.binding != binding
        ):
            msg = "Search Run or input differs from the injected source."
            raise ValueError(msg)
        canonical_uuid(intent.run_id)
        return DumpSearchState(
            run_id=intent.run_id,
            session_id=session_id,
            prompt=intent.prompt,
            manifest_digest=manifest.digest,
            request=request,
        )

    def bind_deps(common: WorkflowServices) -> DumpSearchDeps:
        return DumpSearchDeps(common, source, binding, session_id, principal, manifest.digest, float(dump_timeout_s))

    def validate_state(intent: Intent, state: BaseModel) -> None:
        original = make_state(intent)
        if not isinstance(state, DumpSearchState) or (
            state.run_id,
            state.session_id,
            state.prompt,
            state.manifest_digest,
        ) != (original.run_id, original.session_id, original.prompt, original.manifest_digest):
            msg = "Search checkpoint differs from its exact admitted Run and manifest."
            raise ValueError(msg)
        # Validation is pure: it never binds services or reads the source.
        checked = DumpSearchState.model_validate(state.model_dump(mode="python", warnings=False))
        if checked.request != original.request or (
            checked.snapshot is not None and checked.snapshot.binding != binding
        ):
            msg = "Search continuation changed the request or source snapshot."
            raise ValueError(msg)
        if checked.result is not None and (
            checked.snapshot is None or checked.result != literal_search(checked.snapshot, original.request)
        ):
            msg = "Search continuation changed its derived result."
            raise ValueError(msg)

    return Workflow(
        name=SEARCH_SOURCE,
        title="Companion Dump & Search",
        description="Read an exact catalogue and offer bounded literal matches without game authority.",
        trigger=Trigger(hint="Explicit catalogue search", match=lambda intent: intent.source == SEARCH_SOURCE),
        graph=build_serial_graph(
            nodes=(Dump, LiteralSearch, Report),
            state_type=DumpSearchState,
            deps_type=DumpSearchDeps,
            output_type=SearchResult,
        ),
        start_node=Dump,
        make_state=make_state,
        validate_state=validate_state,
        bind_deps=bind_deps,
        manifest=manifest,
    )
