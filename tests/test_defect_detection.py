"""缺陷数据合成、传统 CV 检测与 IoU 评测测试。"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from detect import detect_defects
from evaluate import evaluate_predictions, iou
from synth_defects import generate_dataset


def test_synthetic_dataset_is_byte_for_byte_reproducible():
    project_root = Path(__file__).resolve().parents[1]
    temporary_root = Path(
        tempfile.mkdtemp(prefix=".tmp-synth-", dir=project_root)
    )
    first_dir = temporary_root / "first"
    second_dir = temporary_root / "second"

    try:
        first_manifest = generate_dataset(
            count=4,
            seed=20260926,
            output_dir=first_dir,
            width=96,
            height=72,
            defect_ratio=1.0,
        )
        second_manifest = generate_dataset(
            count=4,
            seed=20260926,
            output_dir=second_dir,
            width=96,
            height=72,
            defect_ratio=1.0,
        )

        assert first_manifest == second_manifest
        assert first_manifest["is_synthetic"] is True

        for image_name in sorted(first_manifest["images"]):
            first_bytes = (first_dir / "images" / image_name).read_bytes()
            second_bytes = (second_dir / "images" / image_name).read_bytes()
            assert first_bytes == second_bytes

            first_label = json.loads(
                (first_dir / "labels" / f"{image_name}.json").read_text(
                    encoding="utf-8"
                )
            )
            second_label = json.loads(
                (second_dir / "labels" / f"{image_name}.json").read_text(
                    encoding="utf-8"
                )
            )
            assert first_label == second_label
            assert first_label["is_synthetic"] is True
            assert first_label["defects"]

            for defect in first_label["defects"]:
                assert defect["x"] >= 0
                assert defect["y"] >= 0
                assert defect["w"] > 0
                assert defect["h"] > 0
                assert (
                    defect["x"] + defect["w"]
                    <= first_label["width"]
                )
                assert (
                    defect["y"] + defect["h"]
                    <= first_label["height"]
                )
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def test_iou_geometry_cases():
    box = {"x": 0, "y": 0, "w": 10, "h": 10}

    assert iou(box, box) == 1.0
    assert iou(
        box,
        {"x": 20, "y": 20, "w": 5, "h": 5},
    ) == 0.0
    assert iou(
        box,
        {"x": 5, "y": 0, "w": 10, "h": 10},
    ) == pytest.approx(50 / 150)


def test_evaluation_does_not_match_boxes_across_images():
    ground_truth = [
        {
            "image_file": "image_0001.png",
            "defect_type_code": "D01",
            "x": 5,
            "y": 6,
            "w": 20,
            "h": 8,
        }
    ]
    predictions = [
        {**ground_truth[0], "image_file": "image_0002.png"}
    ]

    metrics = evaluate_predictions(ground_truth, predictions)

    assert metrics["overall"]["tp"] == 0
    assert metrics["overall"]["fp"] == 1
    assert metrics["overall"]["fn"] == 1


def test_evaluation_returns_perfect_metrics_for_exact_match():
    ground_truth = [
        {
            "defect_type_code": "D01",
            "x": 5,
            "y": 6,
            "w": 20,
            "h": 8,
        }
    ]
    predictions = [dict(ground_truth[0])]

    metrics = evaluate_predictions(ground_truth, predictions)

    assert metrics["overall"] == {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
    }
    assert metrics["by_defect_type"]["D01"] == metrics["overall"]


def test_detector_finds_known_synthetic_defect():
    project_root = Path(__file__).resolve().parents[1]
    temporary_root = Path(
        tempfile.mkdtemp(prefix=".tmp-detect-", dir=project_root)
    )
    try:
        manifest = generate_dataset(
            count=1,
            seed=20260926,
            output_dir=temporary_root,
            width=160,
            height=120,
            defect_ratio=1.0,
        )
        image_name = manifest["images"][0]
        truth = json.loads(
            (
                temporary_root / "labels" / f"{image_name}.json"
            ).read_text(encoding="utf-8")
        )["defects"][0]

        predictions = detect_defects(
            temporary_root / "images" / image_name
        )

        best_match = max(
            (
                candidate
                for candidate in predictions
                if candidate["defect_type_code"]
                == truth["defect_type_code"]
            ),
            key=lambda candidate: iou(candidate, truth),
            default=None,
        )
        assert best_match is not None
        assert iou(best_match, truth) > 0.5
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
