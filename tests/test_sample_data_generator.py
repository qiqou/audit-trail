import json
from pathlib import Path

from scripts.generate_testdata import PRESETS, generate


def test_small_sample_is_deterministic_and_desensitized(tmp_path):
    output = tmp_path / "脱敏小样本"

    manifest = generate("small", output)

    saved = json.loads((output / "sample_manifest.json").read_text(encoding="utf-8"))
    assert saved == manifest
    assert manifest["sensitive_data"] is False
    assert manifest["counts"]["units"] == PRESETS["small"]["units"]
    assert manifest["counts"]["issues"] == PRESETS["small"]["issues"]
    assert manifest["counts"]["files"] == PRESETS["small"]["files"]
    assert manifest["counts"]["exchange_sessions"] == PRESETS["small"]["exchange_sessions"]
    assert not list(Path(output).rglob("*.xlsx"))


def test_sample_generator_refuses_existing_target(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()

    try:
        generate("small", output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("生成器不应覆盖现有目录")
