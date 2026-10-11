from __future__ import annotations

import tomllib

from lychd.domain.animation.services.adapters.runtimes.openai_compat import OpenAICompatibleRuntimeAdapter
from lychd.extensions.builtin.animator.soulstones import SglangSoulstoneConfig


def test_sample_template_plans_an_executable_server_command() -> None:
    template = SglangSoulstoneConfig.sample_template
    assert template is not None
    soulstone = SglangSoulstoneConfig.model_validate(tomllib.loads(template))
    adapter = OpenAICompatibleRuntimeAdapter(runtime="sglang", config_type=SglangSoulstoneConfig)

    plan = adapter.plan(soulstone)

    assert plan.exec_args[:3] == ["python3", "-m", "sglang.launch_server"]
    assert plan.exec_args == list(soulstone.exec)
