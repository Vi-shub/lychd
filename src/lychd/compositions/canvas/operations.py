"""Pure local Canvas selection, portable import and atomic proposal review."""

from __future__ import annotations

from pydantic import BaseModel

from lychd.compositions.canvas.schemas import (
    MAX_DOCUMENT_BYTES,
    MAX_SCENE_BYTES,
    CanvasBounds,
    CanvasCrop,
    CanvasDocument,
    CanvasPacket,
    CanvasProposal,
    CanvasReviewRequest,
    CanvasReviewResult,
    CanvasScene,
    CanvasSelection,
    refuse,
)

_LOOP_SIDE_PADDING = 35
_LOOP_TOP_PADDING = 80


def _import_record[Record: BaseModel](model: type[Record], value: object, maximum: int) -> Record:
    if isinstance(value, (str, bytes)):
        size = len(value.encode("utf-8")) if isinstance(value, str) else len(value)
        if size > maximum:
            refuse("Canvas import exceeds its byte limit.")
        return model.model_validate_json(value)
    return model.model_validate(value)


def validate_document(value: object) -> CanvasDocument:
    """Validate an entire portable document before the caller replaces a draft."""
    return _import_record(CanvasDocument, value, MAX_DOCUMENT_BYTES)


def _union(bounds: list[CanvasBounds]) -> CanvasBounds | None:
    if not bounds:
        return None
    x = min(item.x for item in bounds)
    y = min(item.y for item in bounds)
    return CanvasBounds(
        x=x,
        y=y,
        width=max(item.x + item.width for item in bounds) - x,
        height=max(item.y + item.height for item in bounds) - y,
    )


def _object_bounds(scene: CanvasScene, object_id: str) -> CanvasBounds | None:
    for stroke in scene.strokes:
        if stroke.id == object_id:
            if stroke.archived:
                return None
            x = min(point.x for point in stroke.points)
            y = min(point.y for point in stroke.points)
            radius = stroke.width / 2
            return CanvasBounds(
                x=x - radius,
                y=y - radius,
                width=max(point.x for point in stroke.points) - x + stroke.width,
                height=max(point.y for point in stroke.points) - y + stroke.width,
            )
    nodes = {node.id: node for node in scene.nodes}
    if object_id in nodes:
        node = nodes[object_id]
        return CanvasBounds(x=node.x, y=node.y, width=node.width, height=node.height)
    for edge in scene.edges:
        if edge.id == object_id:
            if edge.source == edge.target:
                node = nodes[edge.source]
                return CanvasBounds(
                    x=node.x - _LOOP_SIDE_PADDING,
                    y=node.y - _LOOP_TOP_PADDING,
                    width=node.width + 2 * _LOOP_SIDE_PADDING,
                    height=node.height + _LOOP_TOP_PADDING,
                )
            return _union(
                [
                    CanvasBounds(x=node.x, y=node.y, width=node.width, height=node.height)
                    for node in (nodes[edge.source], nodes[edge.target])
                ]
            )
    return None


def object_bounds(scene: CanvasScene, object_id: str) -> CanvasBounds | None:
    """Return a whole object's bounds; archived ink has no visible bounds."""
    return _object_bounds(CanvasScene.model_validate(scene), object_id)


def bounds_of_selection(scene: CanvasScene, object_ids: list[str]) -> CanvasBounds | None:
    """Bound visible selected identities without cutting any source stroke."""
    current = CanvasScene.model_validate(scene)
    return _union([bounds for identity in object_ids if (bounds := _object_bounds(current, identity)) is not None])


def select_in_bounds(scene: CanvasScene, bounds: CanvasBounds) -> list[str]:
    """Select whole visible objects whose bounds intersect a marquee, inclusively."""
    current = CanvasScene.model_validate(scene)
    area = CanvasBounds.model_validate(bounds)
    selected: list[str] = []
    for item in [*current.strokes, *current.nodes, *current.edges]:
        candidate = _object_bounds(current, item.id)
        if candidate is not None and (
            candidate.x <= area.x + area.width
            and candidate.x + candidate.width >= area.x
            and candidate.y <= area.y + area.height
            and candidate.y + candidate.height >= area.y
        ):
            selected.append(item.id)
    return selected


def make_packet(
    scene: CanvasScene,
    object_ids: list[str],
    bounds: CanvasBounds,
    instruction: str,
    crop: CanvasCrop | None = None,
) -> CanvasPacket:
    """Capture exact local records and optional crop; this transmits nothing."""
    current = CanvasScene.model_validate(scene)
    return CanvasPacket(
        format="lychd.canvas.selection/1",
        scene=current,
        selection=CanvasSelection(
            scene_id=current.id,
            revision=current.revision,
            object_ids=object_ids,
            bounds=bounds,
        ),
        instruction=instruction,
        crop=crop,
    )


def _validate_scope(proposal: CanvasProposal, packet: CanvasPacket) -> None:
    scene = packet.scene
    if proposal.scene_id != scene.id or proposal.base_revision != scene.revision:
        refuse("Canvas proposal is stale or belongs to another scene.")
    selected = set(packet.selection.object_ids)
    if set(proposal.selection_ids) != selected:
        refuse("Canvas proposal selection does not match its captured packet.")
    categories = {stroke.id: "stroke" for stroke in scene.strokes}
    categories.update((node.id, "node") for node in scene.nodes)
    categories.update((edge.id, "edge") for edge in scene.edges)
    original_sources = {item.id: set(item.source_stroke_ids) for item in [*scene.nodes, *scene.edges]}
    visible_strokes = {stroke.id for stroke in scene.strokes if not stroke.archived}
    for category, items in (("node", proposal.nodes), ("edge", proposal.edges)):
        for item in items:
            existing = categories.get(item.id)
            if existing is not None and existing != category:
                refuse("Canvas proposal cannot replace an object's category.")
            if existing is not None and item.id not in selected:
                refuse("Canvas proposal cannot update an unselected object.")
            introduced_sources = set(item.source_stroke_ids) - original_sources.get(item.id, set())
            if not introduced_sources <= selected & visible_strokes:
                refuse("Canvas proposal can introduce source references only to selected visible strokes.")
    if not set(proposal.archive_stroke_ids) <= selected & visible_strokes:
        refuse("Canvas proposal can archive only selected visible source strokes.")


def _successor(scene: CanvasScene, proposal: CanvasProposal) -> CanvasScene:
    nodes = {node.id: node for node in scene.nodes}
    nodes.update((node.id, node) for node in proposal.nodes)
    edges = {edge.id: edge for edge in scene.edges}
    edges.update((edge.id, edge) for edge in proposal.edges)
    archived = set(proposal.archive_stroke_ids)
    # A fresh validated aggregate is the only output: no input list is mutated,
    # including when a late endpoint, source reference or size check fails.
    return CanvasScene(
        format=scene.format,
        id=scene.id,
        revision=scene.revision + 1,
        title=scene.title,
        strokes=[
            stroke.model_copy(update={"archived": True}) if stroke.id in archived else stroke
            for stroke in scene.strokes
        ],
        nodes=list(nodes.values()),
        edges=list(edges.values()),
    )


def validate_proposal(value: object, packet: CanvasPacket) -> CanvasProposal:
    """Validate both the permitted write set and the complete prospective result."""
    captured = CanvasPacket.model_validate(packet)
    proposal = _import_record(CanvasProposal, value, MAX_SCENE_BYTES)
    _validate_scope(proposal, captured)
    _successor(captured.scene, proposal)
    return proposal


def apply_proposal(scene: CanvasScene, proposal: CanvasProposal, packet: CanvasPacket) -> CanvasScene:
    """Return one successor revision atomically, refusing stale local snapshots."""
    current = CanvasScene.model_validate(scene)
    captured = CanvasPacket.model_validate(packet)
    if current != captured.scene:
        refuse("Canvas proposal is stale: current scene differs from its captured packet.")
    validated = CanvasProposal.model_validate(proposal)
    _validate_scope(validated, captured)
    return _successor(current, validated)


def review_proposal(request: CanvasReviewRequest) -> CanvasReviewResult:
    """Review a caller-supplied snapshot without server retention or model calls."""
    validated = CanvasReviewRequest.model_validate(request)
    return CanvasReviewResult(
        scene=apply_proposal(validated.packet.scene, validated.proposal, validated.packet),
        summary=validated.proposal.summary,
    )
