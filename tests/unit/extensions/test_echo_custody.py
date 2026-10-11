"""Echo's trusted helper reruns PCM inspection against actual retained material."""

from pathlib import Path
from uuid import uuid4

import pytest

from lychd.domain.artifact_custody import AuthorizationRequest, Decision, ImportSpec, Subject
from lychd.extensions.builtin.echo.custody import admit_pcm_artifact
from lychd.extensions.builtin.echo.material import PcmInspectionError
from lychd.system.services.artifact_custody import LocalArtifactCustody


class Policy:
    def authorize(self, request: AuthorizationRequest) -> Decision:
        return Decision(request=request, allowed=True)


def test_malformed_quarantine_cannot_be_admitted_by_fabricated_facts(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    subject = Subject(identity="trusted-host")
    with LocalArtifactCustody.create(tmp_path, authorizer=Policy()) as custody:
        record = custody.import_bytes(
            b"not a wav",
            ImportSpec(request_id=uuid4(), owner=subject),
            subject=subject,
            purpose="inspect",
        ).record
        with pytest.raises(PcmInspectionError):
            admit_pcm_artifact(custody, record, subject=subject, purpose="admit")
        assert not (tmp_path / (record.request_id.hex + ".admission")).exists()
