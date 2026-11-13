"""Compare the same case set across two variant batches.

A single batch answers "what happened in this configuration". A protocol comparison also needs
"what changed when we turned one thing on", with the case list held fixed. This module renders that
second question: the per-case status and round delta plus the number of outbound requests each run
cost, which is the only place the extra round of a pre-task-negotiation-less protocol shows up.

Usage::

    python -m eval.osp.variant_report base.json e1.json --left-label prose --right-label items
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from eval.osp.pair_report import _mean, _rounds

#: Protocol order used in the summary table.
_PROTOCOL_ORDER: Final[tuple[str, ...]] = ("a2a-t", "a2a")


def load_batch(path: Path) -> dict[str, Any]:
    """Load one persisted batch snapshot."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a batch document")
    return document


def build_variant_report(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    left_label: str = "left",
    right_label: str = "right",
    left_runs_dir: Path | None = None,
    right_runs_dir: Path | None = None,
) -> str:
    """Return the markdown comparison of two batches that ran the same cases."""
    left_items = _items(left)
    right_items = _items(right)
    left_index = {(item.get("caseId"), item.get("protocol")): item for item in left_items}
    right_index = {(item.get("caseId"), item.get("protocol")): item for item in right_items}
    keys = sorted(
        set(left_index) & set(right_index),
        key=lambda key: (str(key[1]), int(key[0]) if isinstance(key[0], int) else 0),
    )
    lines: list[str] = [
        f"# {left_label} vs {right_label}",
        "",
        f"左：`{left.get('batchId')}` ｜ 右：`{right.get('batchId')}`",
        "",
        "只比较两边都存在的用例；请求数按 `wire.jsonl` 的 out 帧计。",
        "",
    ]
    lines += _settings_diff(left, right, left_label, right_label)
    lines += _summary(left_items, right_items, left_label, right_label)
    lines += _matrix(
        keys,
        left_index,
        right_index,
        runs_dirs=(left_runs_dir, right_runs_dir),
        labels=(left_label, right_label),
    )
    lines += _changes(
        keys,
        left_index,
        right_index,
        runs_dirs=(left_runs_dir, right_runs_dir),
        labels=(left_label, right_label),
    )
    return "\n".join(lines).rstrip() + "\n"


def _items(batch: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the batch items as mappings."""
    return [item for item in batch.get("items") or [] if isinstance(item, Mapping)]


def _settings_diff(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    left_label: str,
    right_label: str,
) -> list[str]:
    """Return the manifest comparison: a variant report is only readable if the delta is intended."""
    left_settings = _flat_settings(left.get("settings"))
    right_settings = _flat_settings(right.get("settings"))
    lines = ["## 0. 口径对比", ""]
    if not left_settings and not right_settings:
        lines += ["两边都**未记录**口径 manifest；本报告的差值不可复核。", ""]
        return lines
    if not left_settings or not right_settings:
        missing = left_label if not left_settings else right_label
        lines += [f"**{missing} 未记录口径 manifest**；无法证明两侧只差一个变量。", ""]
        return lines
    keys = sorted(set(left_settings) | set(right_settings))
    changed = [key for key in keys if left_settings.get(key) != right_settings.get(key)]
    if not changed:
        lines += ["两侧口径完全一致。", ""]
        return lines
    lines += [
        "以下键不同。**一个变体实验只应有一个**；多于一个时，差值不能归给其中任何一个。",
        "",
        f"| 键 | {left_label} | {right_label} |",
        "|---|---|---|",
    ]
    for key in changed:
        lines.append(
            f"| {key} | {_display(left_settings.get(key))} | {_display(right_settings.get(key))} |"
        )
    if len(changed) > 1:
        lines += ["", f"> **注意**：共有 {len(changed)} 个键不同，本轮不是单变量实验。"]
    return lines + [""]


def _flat_settings(settings: object) -> dict[str, object]:
    """Flatten the batch manifest so a nested simulator block compares key by key."""
    if not isinstance(settings, Mapping):
        return {}
    flat: dict[str, object] = {}
    for key, value in settings.items():
        if isinstance(value, Mapping):
            for inner_key, inner_value in value.items():
                flat[f"{key}.{inner_key}"] = inner_value
        else:
            flat[str(key)] = value
    return flat


def _display(value: object) -> str:
    """Render one manifest value compactly."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"`{value}`"


def _summary(
    left_items: Sequence[Mapping[str, Any]],
    right_items: Sequence[Mapping[str, Any]],
    left_label: str,
    right_label: str,
) -> list[str]:
    """Return the per-protocol totals of both variants."""
    lines = [
        "## 1. 汇总",
        "",
        "| 变体 | 协议 | 项数 | passed | 均值轮次 | 轮次合计 | 请求合计 |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, items in ((left_label, left_items), (right_label, right_items)):
        for protocol in _protocols(items):
            rows = [row for row in items if str(row.get("protocol")) == protocol]
            rounds = [value for value in (_rounds(row) for row in rows) if value is not None]
            lines.append(
                "| {label} | {protocol} | {count} | {passed} | {mean} | {total} | {requests} |".format(
                    label=label,
                    protocol=protocol,
                    count=len(rows),
                    passed=sum(1 for row in rows if row.get("status") == "passed"),
                    mean=_mean(rounds),
                    total=sum(rounds) if rounds else "—",
                    requests="见矩阵" if rows else "—",
                )
            )
    return lines + [""]


def _matrix(
    keys: Sequence[tuple[object, object]],
    left_index: Mapping[tuple[object, object], Mapping[str, Any]],
    right_index: Mapping[tuple[object, object], Mapping[str, Any]],
    *,
    runs_dirs: tuple[Path | None, Path | None],
    labels: tuple[str, str],
) -> list[str]:
    """Return the per-case matrix."""
    left_label, right_label = labels
    lines = [
        "## 2. 逐用例",
        "",
        f"| case | 协议 | {left_label} | {right_label} | {left_label} 轮次 | {right_label} 轮次 | "
        f"{left_label} 请求 | {right_label} 请求 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key in keys:
        left = left_index[key]
        right = right_index[key]
        lines.append(
            "| {case} | {protocol} | {left} | {right} | {lr} | {rr} | {lq} | {rq} |".format(
                case=key[0],
                protocol=key[1],
                left=left.get("status"),
                right=right.get("status"),
                lr=_show(_rounds(left)),
                rr=_show(_rounds(right)),
                lq=_requests(left, runs_dirs[0]),
                rq=_requests(right, runs_dirs[1]),
            )
        )
    if not keys:
        lines.append("| — | — | — | — | — | — | — | — |")
    return lines + [""]


def _changes(
    keys: Sequence[tuple[object, object]],
    left_index: Mapping[tuple[object, object], Mapping[str, Any]],
    right_index: Mapping[tuple[object, object], Mapping[str, Any]],
    *,
    runs_dirs: tuple[Path | None, Path | None],
    labels: tuple[str, str],
) -> list[str]:
    """Return only the cases whose status, rounds or request count moved."""
    left_label, right_label = labels
    lines = [
        "## 3. 变化明细",
        "",
        "| case | 协议 | 状态 | 轮次 | 请求 |",
        "|---|---|---|---|---|",
    ]
    changed = 0
    for key in keys:
        left = left_index[key]
        right = right_index[key]
        status = f"{left.get('status')} → {right.get('status')}"
        rounds = f"{_show(_rounds(left))} → {_show(_rounds(right))}"
        requests = f"{_requests(left, runs_dirs[0])} → {_requests(right, runs_dirs[1])}"
        if (
            left.get("status") == right.get("status")
            and _rounds(left) == _rounds(right)
            and _requests(left, runs_dirs[0]) == _requests(right, runs_dirs[1])
        ):
            continue
        changed += 1
        lines.append(f"| {key[0]} | {key[1]} | {status} | {rounds} | {requests} |")
    if changed == 0:
        lines.append(f"| — | — | 无变化（{left_label} 与 {right_label} 逐项一致） | — | — |")
    return lines + [""]


def _protocols(items: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the protocols present, in a stable order."""
    present = {str(item.get("protocol")) for item in items}
    ordered = [protocol for protocol in _PROTOCOL_ORDER if protocol in present]
    return ordered + sorted(present - set(ordered))


def _requests(item: Mapping[str, Any], runs_dir: Path | None) -> int | str:
    """Return how many outbound frames the run sent, ``—`` when the run directory is missing."""
    if runs_dir is None:
        return "—"
    run_id = item.get("runId")
    if not isinstance(run_id, str) or not run_id:
        return "—"
    total, found = _count_outbound(runs_dir / run_id / "wire.jsonl")
    return total if found else "—"


def _count_outbound(path: Path) -> tuple[int, bool]:
    """Count the ``dir == "out"`` frames of one run's wire log."""
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return 0, False
    total = 0
    with handle:
        for line in handle:
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if isinstance(frame, Mapping) and frame.get("dir") == "out":
                total += 1
    return total, True


def _show(value: int | None) -> str:
    """Render one optional integer."""
    return "—" if value is None else str(value)


def main(argv: Iterable[str] | None = None) -> int:
    """Compare two batch files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--left-label", default="left")
    parser.add_argument("--right-label", default="right")
    parser.add_argument("--left-runs-dir", type=Path, default=None, help="default: the left batch file's grandparent")
    parser.add_argument(
        "--right-runs-dir",
        type=Path,
        default=None,
        help="default: the right batch file's grandparent",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        left = load_batch(args.left)
        right = load_batch(args.right)
    except (OSError, ValueError) as exc:
        print(f"[variant_report] {exc}", file=sys.stderr)
        return 2
    report = build_variant_report(
        left,
        right,
        left_label=args.left_label,
        right_label=args.right_label,
        left_runs_dir=args.left_runs_dir or args.left.parent.parent,
        right_runs_dir=args.right_runs_dir or args.right.parent.parent,
    )
    if args.out is None:
        sys.stdout.write(report)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"[variant_report] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
