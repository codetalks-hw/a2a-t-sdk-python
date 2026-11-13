"""Tests of the two-variant batch comparison."""

from __future__ import annotations

import json
from pathlib import Path

from eval.osp.variant_report import build_variant_report


def _wire(runs_dir: Path, run_id: str, requests: int) -> None:
    """Write the outbound request frames of one run."""
    directory = runs_dir / run_id
    directory.mkdir(parents=True, exist_ok=True)
    frames = [
        {"dir": "out", "method": "POST", "url": "https://nms.example/a2a/json/message:stream"}
        for _ in range(requests)
    ]
    frames.append({"dir": "in", "status": 200})
    (directory / "wire.jsonl").write_text(
        "\n".join(json.dumps(frame) for frame in frames) + "\n", encoding="utf-8"
    )


def _batch(batch_id: str, *, status: str, rounds: int, run_id: str) -> dict[str, object]:
    """Return a one-item batch snapshot of the plain A2A arm."""
    return {
        "batchId": batch_id,
        "items": [
            {
                "caseId": 7,
                "protocol": "a2a",
                "status": status,
                "runId": run_id,
                "stages": {"negotiate": {"actual": {"rounds": rounds}}},
            }
        ],
    }


def test_the_report_shows_the_round_and_request_delta(tmp_path: Path) -> None:
    """The point of the comparison is the delta, so a changed row must carry both sides."""
    left_runs = tmp_path / "left"
    right_runs = tmp_path / "right"
    _wire(left_runs, "run-left", 3)
    _wire(right_runs, "run-right", 2)

    report = build_variant_report(
        _batch("batch-left", status="passed", rounds=3, run_id="run-left"),
        _batch("batch-right", status="passed", rounds=1, run_id="run-right"),
        left_label="prose",
        right_label="items",
        left_runs_dir=left_runs,
        right_runs_dir=right_runs,
    )

    assert "| prose | a2a | 1 | 1 | 3 | 3 | 见矩阵 |" in report
    assert "| items | a2a | 1 | 1 | 1 | 1 | 见矩阵 |" in report
    assert "| 7 | a2a | passed → passed | 3 → 1 | 3 → 2 |" in report


def test_an_identical_pair_reports_no_change(tmp_path: Path) -> None:
    """A variant that changed nothing must say so instead of printing an empty table."""
    runs = tmp_path / "runs"
    _wire(runs, "run-same", 2)

    report = build_variant_report(
        _batch("left", status="passed", rounds=1, run_id="run-same"),
        _batch("right", status="passed", rounds=1, run_id="run-same"),
        left_runs_dir=runs,
        right_runs_dir=runs,
    )

    assert "无变化" in report


def test_a_case_missing_from_one_side_is_not_compared(tmp_path: Path) -> None:
    """Only the shared case list is comparable; a one-sided case would fake a delta."""
    runs = tmp_path / "runs"
    _wire(runs, "run-one", 2)
    left = _batch("left", status="passed", rounds=1, run_id="run-one")
    right = _batch("right", status="passed", rounds=1, run_id="run-one")
    right_items = right["items"]
    assert isinstance(right_items, list)
    right_items[0]["caseId"] = 8  # ty: ignore[invalid-assignment]

    report = build_variant_report(left, right, left_runs_dir=runs, right_runs_dir=runs)

    assert "| 7 | a2a |" not in report
    assert "| — | — | — | — | — | — | — | — |" in report


def test_a_single_differing_switch_is_reported_as_the_variable(tmp_path: Path) -> None:
    """A variant report must show which keys differ; exactly one is what makes it a variant."""
    runs = tmp_path / "runs"
    _wire(runs, "run-one", 2)
    left = _batch("left", status="passed", rounds=2, run_id="run-one")
    right = _batch("right", status="passed", rounds=1, run_id="run-one")
    left["settings"] = {"stopAfter": "accepted", "simulator": {"missing_items": False}}
    right["settings"] = {"stopAfter": "accepted", "simulator": {"missing_items": True}}

    report = build_variant_report(left, right, left_runs_dir=runs, right_runs_dir=runs)

    assert "## 0. 口径对比" in report
    assert "| simulator.missing_items | false | true |" in report
    assert "单变量实验" not in report


def test_several_differing_switches_are_flagged_as_not_a_single_variable(tmp_path: Path) -> None:
    """Two changed switches make the delta unattributable, so the report has to say so."""
    runs = tmp_path / "runs"
    _wire(runs, "run-one", 2)
    left = _batch("left", status="passed", rounds=2, run_id="run-one")
    right = _batch("right", status="passed", rounds=1, run_id="run-one")
    left["settings"] = {"pureAnswerSource": "script", "simulator": {"missing_items": False}}
    right["settings"] = {"pureAnswerSource": "agent", "simulator": {"missing_items": True}}

    report = build_variant_report(left, right, left_runs_dir=runs, right_runs_dir=runs)

    assert "共有 2 个键不同" in report


def test_a_missing_manifest_on_one_side_is_stated(tmp_path: Path) -> None:
    """A batch that predates the manifest cannot be compared with one that has it."""
    runs = tmp_path / "runs"
    _wire(runs, "run-one", 2)
    left = _batch("left", status="passed", rounds=2, run_id="run-one")
    right = _batch("right", status="passed", rounds=1, run_id="run-one")
    right["settings"] = {"stopAfter": "accepted"}

    report = build_variant_report(left, right, left_runs_dir=runs, right_runs_dir=runs)

    assert "left 未记录口径 manifest" in report
