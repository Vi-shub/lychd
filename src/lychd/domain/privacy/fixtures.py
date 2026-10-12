"""Public synthetic fixtures for an offline preparation proof, never a live source adapter."""

from uuid import UUID

from lychd.domain.privacy.contracts import (
    LANES,
    CutBinding,
    PublicToken,
    SensitiveSpan,
    TextMaterial,
    UtilitySpan,
    WholeProjection,
    digest,
)
from lychd.domain.privacy.labels import PrivacyCategory, PrivacyClass, PrivatizationLabel


def diagnostic_fixture() -> WholeProjection:
    """Cover every outbound lane with utility and a plainly synthetic private value."""
    prefix: tuple[PublicToken, ...] = ("Build ", "node-a", " depends on ", "node-b", " failed", "; ", "owner: ")
    suffix: tuple[PublicToken, ...] = ("; ", "diagnostic: ", "E001", "; ", "citation: ", "[1]")
    text = "".join(prefix)
    start = len(text)
    text += "SYNTHETIC-PERSON-017"
    end = len(text)
    text += "".join(suffix)
    utility: list[UtilitySpan] = []
    cursor = 0
    for token in prefix:
        utility.append(UtilitySpan(start=cursor, end=cursor + len(token), token=token))
        cursor += len(token)
    cursor = end
    for token in suffix:
        utility.append(UtilitySpan(start=cursor, end=cursor + len(token), token=token))
        cursor += len(token)
    materials = {
        lane: (
            TextMaterial(
                source_id=UUID(int=index + 1),
                text=text,
                label=PrivatizationLabel(
                    classification=PrivacyClass.PRIVATE,
                    weight=0.8,
                    categories=frozenset({PrivacyCategory.DIRECT_IDENTIFIER}),
                    subjects=frozenset({UUID(int=101)}),
                    lineage_known=True,
                ),
                sensitive_spans=(SensitiveSpan(start=start, end=end, category=PrivacyCategory.DIRECT_IDENTIFIER),),
                utility_spans=tuple(utility),
            ),
        )
        for index, lane in enumerate(LANES)
    }
    return WholeProjection.model_validate(materials)


def fixture_binding() -> CutBinding:
    """Return a public synthetic binding with no remote destination or credential."""
    return CutBinding(
        consumer_kind="export",
        consumer_id=UUID(int=201),
        principal_id=UUID(int=202),
        sigil_digest=digest("synthetic-sigil"),
        run_id=UUID(int=203),
        occurrence_id=UUID(int=204),
        attempt_id=UUID(int=205),
        purpose_id=UUID(int=206),
        target_digest=digest("offline-synthetic-diagnostic-export"),
    )
