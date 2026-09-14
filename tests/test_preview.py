import json
from pathlib import Path

import pytest

from career_radar.demo import run_demo
from career_radar.preview import export_preview


def test_dashboard_export_is_synthetic_private_and_escaped(tmp_path: Path):
    result = run_demo()
    result.jobs[0].title = "</script><script>alert(1)</script>"
    path = export_preview(result, tmp_path, synthetic=True)
    content = path.read_text()
    assert "</script><script>alert(1)</script>" not in content
    assert "\\u003c/script\\u003e" in content
    assert "Synthetic demo" in content
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "email-preview.html").exists()
    assert json.loads((tmp_path / "run-summary.json").read_text())["canonical_jobs"] == 9


def test_demo_labels_cannot_be_exported_as_live(tmp_path: Path):
    with pytest.raises(ValueError, match="SYNTHETIC_ONLY"):
        export_preview(run_demo(), tmp_path, synthetic=False)
