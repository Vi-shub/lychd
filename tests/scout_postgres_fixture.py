"""Disposable cached PostgreSQL with pathname IPC and no IP network exposure.

The caller must verify its opt-in isolated network namespace before entering.
Only the literal local Docker socket is used; no daemon environment, image pull,
operator database, or host TCP port participates. These are test fixture effects,
not a LychD containment receipt.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch
from uuid import uuid4

if TYPE_CHECKING:
    from docker import DockerClient
    from docker.models.containers import Container

_IMAGE = "pgvector/pgvector:pg18-trixie"
_LABEL = "lychd.scout-composed-proof"
_REQUEST_SECONDS = 10
_READY_SECONDS = 30


@dataclass(frozen=True)
class IsolatedPostgres:
    """Expose the owned socket and mutable observations including final cleanup."""

    socket_directory: Path
    evidence: dict[str, Any]


def _client() -> DockerClient:
    """Construct without Docker config, credential helpers, netrc or proxy inheritance."""
    import docker
    from docker.constants import DEFAULT_DOCKER_API_VERSION

    # A nonempty config prevents the SDK authentication loader's file fallback.
    with patch("docker.utils.config.load_general_config", return_value={"auths": {}}):
        client = docker.DockerClient(
            base_url="unix:///var/run/docker.sock",
            version=DEFAULT_DOCKER_API_VERSION,
            timeout=_REQUEST_SECONDS,
        )
    # Explicit version suppresses constructor I/O until Requests inheritance is off.
    client.api.trust_env = False
    return client


def _ready(client: DockerClient, container: Container) -> None:
    """Require the final server, not the entrypoint's temporary bootstrap server."""
    deadline = time.monotonic() + _READY_SECONDS
    api = cast("Any", client.api)  # Docker SDK leaves exec_create parameters untyped.
    while time.monotonic() < deadline:
        container.reload()
        if container.status != "running":
            msg = "Owned PostgreSQL container stopped before readiness."
            raise RuntimeError(msg)
        created = api.exec_create(
            container.id,
            [
                "sh",
                "-c",
                'test "$(cat /proc/1/comm)" = postgres && exec pg_isready -q -t 2 -h /scout-socket -U scout -d scout',
            ],
            stdout=False,
            stderr=False,
        )
        api.exec_start(created["Id"], detach=True)
        while time.monotonic() < deadline:
            observed = api.exec_inspect(created["Id"])
            if not observed["Running"]:
                if observed["ExitCode"] == 0:
                    return
                break
            time.sleep(0.1)
        time.sleep(0.1)
    msg = "Owned PostgreSQL readiness deadline expired."
    raise TimeoutError(msg)


def _remove(client: DockerClient, name: str, identity: str | None, evidence: dict[str, Any]) -> None:
    """Reconcile even an ambiguous create, then confirm owned container/volume removal."""
    from docker.errors import NotFound

    try:
        owned = client.containers.get(identity or name)
    except NotFound:
        if identity is not None:
            msg = "Owned PostgreSQL container disappeared before cleanup observation."
            raise RuntimeError(msg) from None
        evidence["container_created"] = False
        evidence["container_removed"] = True
        evidence["anonymous_volumes_removed"] = True
        return
    assert owned.name == name
    assert owned.attrs["Config"]["Labels"].get(_LABEL) == name
    owned_id = owned.id
    assert isinstance(owned_id, str)
    evidence.update(container_created=True, container_id=owned_id)
    volumes = [mount["Name"] for mount in owned.attrs["Mounts"] if mount["Type"] == "volume"]
    assert len(volumes) <= 4
    evidence["anonymous_volume_count"] = len(volumes)
    owned.remove(force=True, v=True)
    try:
        client.containers.get(owned_id)
    except NotFound:
        evidence["container_removed"] = True
    else:
        msg = "Owned PostgreSQL container remains after removal."
        raise RuntimeError(msg)
    for volume in volumes:
        try:
            client.volumes.get(volume)
        except NotFound:
            continue
        msg = "Owned PostgreSQL anonymous volume remains after container removal."
        raise RuntimeError(msg)
    evidence["anonymous_volumes_removed"] = True


@contextmanager
def isolated_postgres() -> Generator[IsolatedPostgres]:
    """Own one cached-image database until confirmed removal, including failed startup."""
    assert Path("/var/run/docker.sock").is_socket(), "A local pathname Docker socket is required."
    client = _client()
    name = "lychd-scout-composed-" + uuid4().hex
    identity: str | None = None
    evidence: dict[str, Any] = {
        "daemon": "local-docker-unix-socket",
        "ambient_configuration": "disabled",
        "image": _IMAGE,
        "container_created": False,
        "container_removed": False,
        "anonymous_volumes_removed": False,
        "socket_directory_removed": False,
    }
    try:
        image = client.images.get(_IMAGE)
        image_id = image.id
        assert isinstance(image_id, str)
        evidence["image_id"] = image_id
        directory = Path(tempfile.mkdtemp(prefix="lychd-scout-pg-"))
        try:
            socket = directory / "socket"
            socket.mkdir(mode=0o777)
            # Permit the image's mapped postgres UID only within this owned bind.
            socket.chmod(0o777)
            container = client.containers.create(
                image_id,
                name=name,
                labels={_LABEL: name},
                command=[
                    "postgres",
                    "-c",
                    "unix_socket_directories=/var/run/postgresql,/scout-socket",
                    "-c",
                    "listen_addresses=",
                ],
                environment={"POSTGRES_USER": "scout", "POSTGRES_PASSWORD": "fixture", "POSTGRES_DB": "scout"},
                network_mode="none",
                use_config_proxy=False,
                volumes={str(socket): {"bind": "/scout-socket", "mode": "rw,Z"}},
                mem_limit="512m",
                nano_cpus=1_000_000_000,
                pids_limit=128,
            )
            identity = container.id
            evidence.update(container_created=True, container_id=identity)
            container.start()
            _ready(client, container)
            container.reload()
            host = container.attrs["HostConfig"]
            assert host["NetworkMode"] == "none"
            assert not host["PortBindings"]
            assert host["Memory"] == 512 * 1024 * 1024
            assert host["NanoCpus"] == 1_000_000_000
            assert host["PidsLimit"] == 128
            evidence.update(network="none", published_ports=False, ready=True)
            yield IsolatedPostgres(socket, evidence)
        finally:
            _remove(client, name, identity, evidence)
            shutil.rmtree(directory)
            assert not directory.exists()
            evidence["socket_directory_removed"] = True
    finally:
        client.close()
