"""缺陷检测结果与合成标注真值的 IoU 评测。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from detect import detect_defects

Box = Mapping[str, Any]
GroundTruth = list[dict[str, Any]]
Predictions = list[dict[str, Any]]
Detector = Callable[[str | Path], Predictions]


def iou(box_a: Box, box_b: Box) -> float:
    """计算两个 `x/y/w/h` 边界框的交并比。"""
    ax1 = float(box_a["x"])
    ay1 = float(box_a["y"])
    ax2 = ax1 + float(box_a["w"])
    ay2 = ay1 + float(box_a["h"])
    bx1 = float(box_b["x"])
    by1 = float(box_b["y"])
    bx2 = bx1 + float(box_b["w"])
    by2 = by1 + float(box_b["h"])

    intersection_width = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    intersection_height = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = intersection_width * intersection_height

    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


def _metric_counts(tp: int, fp: int, fn: int) -> dict[str, Any]:
    """把 TP/FP/FN 汇总为精确率、召回率和 F1。"""
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _match_by_type(
    ground_truth: GroundTruth,
    predictions: Predictions,
    iou_threshold: float,
    defect_type_code: str | None = None,
) -> tuple[int, int, int]:
    """按类别执行最大一对一匹配，避免贪心抢占导致可配对被丢弃。"""
    if defect_type_code is None:
        selected_ground_truth = ground_truth
        selected_predictions = predictions
    else:
        selected_ground_truth = [
            item
            for item in ground_truth
            if item.get("defect_type_code") == defect_type_code
        ]
        selected_predictions = [
            item
            for item in predictions
            if item.get("defect_type_code") == defect_type_code
        ]

    candidate_predictions: dict[int, list[int]] = {}
    for truth_index, truth in enumerate(selected_ground_truth):
        candidates = []
        for prediction_index, prediction in enumerate(
            selected_predictions
        ):
            if truth.get("defect_type_code") != prediction.get(
                "defect_type_code"
            ):
                continue
            truth_image = truth.get("image_file")
            prediction_image = prediction.get("image_file")
            if (
                truth_image is not None
                and prediction_image is not None
                and truth_image != prediction_image
            ):
                continue
            overlap = iou(truth, prediction)
            if overlap >= iou_threshold:
                candidates.append((overlap, prediction_index))
        candidate_predictions[truth_index] = [
            prediction_index
            for _, prediction_index in sorted(
                candidates,
                reverse=True,
            )
        ]

    matched_truth_by_prediction: dict[int, int] = {}

    def assign(truth_index: int, seen_predictions: set[int]) -> bool:
        for prediction_index in candidate_predictions[truth_index]:
            if prediction_index in seen_predictions:
                continue
            seen_predictions.add(prediction_index)
            previous_truth = matched_truth_by_prediction.get(
                prediction_index
            )
            if previous_truth is None or assign(
                previous_truth,
                seen_predictions,
            ):
                matched_truth_by_prediction[prediction_index] = truth_index
                return True
        return False

    truth_order = sorted(
        candidate_predictions,
        key=lambda truth_index: len(
            candidate_predictions[truth_index]
        ),
    )
    for truth_index in truth_order:
        assign(truth_index, set())

    tp = len(matched_truth_by_prediction)
    fp = len(selected_predictions) - tp
    fn = len(selected_ground_truth) - tp
    return tp, fp, fn


def evaluate_predictions(
    ground_truth: GroundTruth,
    predictions: Predictions,
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    """按缺陷类别统计检测指标，类别不匹配时记为漏检和误报。"""
    if not 0.0 <= iou_threshold <= 1.0:
        raise ValueError("iou_threshold 必须在 0 到 1 之间")

    tp, fp, fn = _match_by_type(
        ground_truth,
        predictions,
        iou_threshold,
    )
    defect_codes = sorted(
        {
            item.get("defect_type_code")
            for item in ground_truth + predictions
            if item.get("defect_type_code")
        }
    )
    by_defect_type = {}
    for defect_code in defect_codes:
        type_tp, type_fp, type_fn = _match_by_type(
            ground_truth,
            predictions,
            iou_threshold,
            defect_code,
        )
        by_defect_type[defect_code] = _metric_counts(
            type_tp,
            type_fp,
            type_fn,
        )

    return {
        "iou_threshold": iou_threshold,
        "overall": _metric_counts(tp, fp, fn),
        "by_defect_type": by_defect_type,
    }


def evaluate_dataset(
    dataset_dir: str | Path,
    *,
    iou_threshold: float = 0.5,
    detector: Detector = detect_defects,
) -> dict[str, Any]:
    """在合成数据集上运行检测器，并按类别汇总 IoU 匹配指标。"""
    dataset_path = Path(dataset_dir)
    labels_path = dataset_path / "labels"
    images_path = dataset_path / "images"
    label_files = sorted(labels_path.glob("*.png.json"))
    if not label_files:
        raise ValueError(f"目录中没有合成标签：{labels_path}")

    all_ground_truth: Predictions = []
    all_predictions: Predictions = []
    image_count = 0

    for label_file in label_files:
        label = json.loads(label_file.read_text(encoding="utf-8"))
        if label.get("is_synthetic") is not True:
            raise ValueError(
                f"标签未明确标记为合成数据：{label_file.name}"
            )

        image_path = images_path / label["image_file"]
        if not image_path.is_file():
            raise FileNotFoundError(f"标签对应图片不存在：{image_path}")

        image_name = label["image_file"]
        ground_truth = [
            {**item, "image_file": image_name}
            for item in label.get("defects", [])
        ]
        predictions = [
            {**item, "image_file": image_name}
            for item in detector(image_path)
        ]
        all_ground_truth.extend(ground_truth)
        all_predictions.extend(predictions)
        image_count += 1

    metrics = evaluate_predictions(
        all_ground_truth,
        all_predictions,
        iou_threshold=iou_threshold,
    )
    return {
        "title": "合成缺陷检测评测报告",
        "is_synthetic": True,
        "data_source": "程序合成数据，非真实工业图像",
        "image_count": image_count,
        "ground_truth_count": len(all_ground_truth),
        "prediction_count": len(all_predictions),
        **metrics,
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    """把评测结果渲染为适合交付的 Markdown 报告。"""
    overall = report["overall"]
    lines = [
        "# 合成缺陷检测评测报告",
        "",
        (
            "> **数据集为合成数据，非真实工业图像。** "
            "以下指标只用于验证本项目的标注、传统 CV 检测和 IoU "
            "评测流程，不能解释为真实工业缺陷检测能力。"
        ),
        "",
        "## 数据与口径",
        "",
        f"- 图像数：{report['image_count']}",
        f"- 真值框数：{report['ground_truth_count']}",
        f"- 预测框数：{report['prediction_count']}",
        f"- IoU 匹配阈值：{report['iou_threshold']}",
        "- 匹配规则：类别一致且 IoU 达到阈值，最大一对一匹配",
        "",
        "## 总体指标",
        "",
        "| TP | FP | FN | Precision | Recall | F1 |",
        "|---:|---:|---:|---:|---:|---:|",
        (
            f"| {overall['tp']} | {overall['fp']} | "
            f"{overall['fn']} | {overall['precision']:.4f} | "
            f"{overall['recall']:.4f} | {overall['f1']:.4f} |"
        ),
        "",
        "## 分类指标",
        "",
        "| 类别 | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    for defect_code, metrics in report["by_defect_type"].items():
        lines.append(
            f"| {defect_code} | {metrics['tp']} | {metrics['fp']} | "
            f"{metrics['fn']} | {metrics['precision']:.4f} | "
            f"{metrics['recall']:.4f} | {metrics['f1']:.4f} |"
        )

    lines.extend(
        [
            "",
            "## 方法与局限",
            "",
            (
                "检测器使用局部高斯背景估计、残差阈值、形态学连接和"
                "连通域几何分类，不包含深度学习或训练过程。"
            ),
            "",
            (
                "合成缺陷具有规则几何形状、均匀背景和受控噪声；"
                "真实工业场景还存在复杂划痕形态、材质纹理、反光、"
                "光照变化、遮挡、样本不均衡和域差异。因此报告中的"
                "高指标只说明传统 CV 能识别本生成器的简化模式，"
                "不能据此推断产线可用性。"
            ),
            "",
            (
                "本项目的定位是跑通“数据合成/清洗、标注、复核、"
                "传统 CV 检测、IoU 评测”的完整流程，不是交付可用的"
                "工业缺陷检测系统。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在合成缺陷数据集上运行传统 CV 检测并输出评测报告。"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data") / "defect_synth",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data") / "defect_synth" / "evaluation_report.md",
    )
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 0.0 <= args.iou_threshold <= 1.0:
        raise SystemExit("--iou-threshold 必须在 0 到 1 之间")

    report = evaluate_dataset(
        args.dataset,
        iou_threshold=args.iou_threshold,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.suffix.lower() == ".json":
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        args.output.write_text(
            render_markdown_report(report),
            encoding="utf-8",
        )

    overall = report["overall"]
    print(
        "数据集为合成数据，非真实工业图像；"
        f"precision={overall['precision']:.4f}，"
        f"recall={overall['recall']:.4f}，"
        f"f1={overall['f1']:.4f}，报告={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
