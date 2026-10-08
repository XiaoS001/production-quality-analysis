"""基于 Pillow + numpy 的可解释传统 CV 缺陷检测。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

DEFECT_METADATA = {
    "D01": "外观划痕",
    "D02": "尺寸超差",
    "D03": "装配不到位",
    "D04": "功能失效",
}

RESIDUAL_THRESHOLD = 25.0
MIN_COMPONENT_AREA = 8
MAX_COMPONENT_AREA_RATIO = 0.35


def _morph_close(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    """用最大/最小滤波近似闭运算，连接阈值断裂并抹掉单像素噪点。"""
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    size = radius * 2 + 1
    image = image.filter(ImageFilter.MaxFilter(size))
    image = image.filter(ImageFilter.MinFilter(size))
    return np.asarray(image) > 0


def _connected_components(mask: np.ndarray) -> list[np.ndarray]:
    """提取 8 邻域连通域，避免只装 scipy/OpenCV 带来的额外依赖。"""
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: list[np.ndarray] = []
    neighbors = (
        (-1, -1),
        (-1, 0),
        (-1, 1),
        (0, -1),
        (0, 1),
        (1, -1),
        (1, 0),
        (1, 1),
    )

    for start_y, start_x in zip(*np.nonzero(mask & ~visited)):
        if visited[start_y, start_x]:
            continue
        stack = [(int(start_y), int(start_x))]
        visited[start_y, start_x] = True
        points: list[tuple[int, int]] = []

        while stack:
            y, x = stack.pop()
            points.append((y, x))
            for delta_y, delta_x in neighbors:
                next_y = y + delta_y
                next_x = x + delta_x
                if (
                    0 <= next_y < height
                    and 0 <= next_x < width
                    and mask[next_y, next_x]
                    and not visited[next_y, next_x]
                ):
                    visited[next_y, next_x] = True
                    stack.append((next_y, next_x))

        components.append(np.asarray(points, dtype=np.int32))

    return components


def _component_features(
    component: np.ndarray,
    width: int,
    height: int,
) -> dict[str, float | int]:
    """计算连通域的面积、填充率、径向离散度和角落占用率。"""
    area = int(component.shape[0])
    local_mask = np.zeros((height, width), dtype=bool)
    local_mask[
        component[:, 0] - component[:, 0].min(),
        component[:, 1] - component[:, 1].min(),
    ] = True

    ys, xs = np.nonzero(local_mask)
    radii = np.sqrt(
        (xs - xs.mean()) ** 2 + (ys - ys.mean()) ** 2
    )
    radial_cv = (
        float(radii.std() / radii.mean()) if radii.mean() else 0.0
    )

    corner_width = max(1, round(width * 0.2))
    corner_height = max(1, round(height * 0.2))
    corner_pixels = np.concatenate(
        [
            local_mask[:corner_height, :corner_width].ravel(),
            local_mask[:corner_height, -corner_width:].ravel(),
            local_mask[-corner_height:, :corner_width].ravel(),
            local_mask[-corner_height:, -corner_width:].ravel(),
        ]
    )
    corner_fraction = float(corner_pixels.mean())

    return {
        "area": area,
        "aspect_ratio": max(width, height) / max(1, min(width, height)),
        "fill_ratio": area / max(1, width * height),
        "radial_cv": radial_cv,
        "corner_fraction": corner_fraction,
    }


def _classify_component(features: dict[str, float | int]) -> str:
    """按可解释几何特征将连通域映射到现有 defect_type 编码。

    D01 划痕细长；D03 装配异常呈低填充弧形/空心结构。D04 圆盘
    与 D02 实心矩形都用角点占比和径向离散度区分：矩形四角更
    充实，而圆盘边缘到质心的距离更集中。
    """
    aspect_ratio = float(features["aspect_ratio"])
    fill_ratio = float(features["fill_ratio"])
    radial_cv = float(features["radial_cv"])
    corner_fraction = float(features["corner_fraction"])

    if aspect_ratio >= 2.7 and fill_ratio <= 0.65:
        return "D01"
    if (
        fill_ratio >= 0.65
        and radial_cv >= 0.33
        and corner_fraction <= 0.20
    ):
        return "D04"
    if fill_ratio >= 0.60:
        return "D02"
    if fill_ratio <= 0.48:
        return "D03"
    return "D04"


def detect_defects(image_path: str | Path) -> list[dict[str, Any]]:
    """检测单张 PNG 中的缺陷框，返回类别、边界框和可解释特征。"""
    image = Image.open(image_path).convert("RGB")
    grayscale = np.asarray(image.convert("L"), dtype=np.float32)

    # 大半径高斯平滑只保留缓慢照度变化；残差突出局部缺陷。
    local_background = np.asarray(
        image.convert("L").filter(ImageFilter.GaussianBlur(radius=7)),
        dtype=np.float32,
    )
    residual = grayscale - local_background
    mask = np.abs(residual) >= RESIDUAL_THRESHOLD
    mask = _morph_close(mask, radius=1)

    image_height, image_width = mask.shape
    max_area = int(image_width * image_height * MAX_COMPONENT_AREA_RATIO)
    detections: list[dict[str, Any]] = []

    for component in _connected_components(mask):
        area = int(component.shape[0])
        if area < MIN_COMPONENT_AREA or area > max_area:
            continue

        ys = component[:, 0]
        xs = component[:, 1]
        x = int(xs.min())
        y = int(ys.min())
        width = int(xs.max() - x + 1)
        height = int(ys.max() - y + 1)
        if width < 2 or height < 1:
            continue

        features = _component_features(component, width, height)
        defect_code = _classify_component(features)
        component_residual = np.abs(residual[ys, xs])
        confidence = min(
            0.99,
            max(0.05, float(component_residual.mean()) / 100.0),
        )
        detections.append(
            {
                "defect_type_code": defect_code,
                "defect_type_name": DEFECT_METADATA[defect_code],
                "x": x,
                "y": y,
                "w": width,
                "h": height,
                "confidence": round(confidence, 4),
                "features": {
                    key: round(value, 4)
                    if isinstance(value, float)
                    else value
                    for key, value in features.items()
                },
            }
        )

    return sorted(
        detections,
        key=lambda item: (item["y"], item["x"], item["defect_type_code"]),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "传统 CV 缺陷检测：局部高斯背景估计 + 残差阈值 + "
            "形态学连接 + 连通域几何分类。"
        )
    )
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    detections = detect_defects(args.image)
    print(
        json.dumps(
            detections,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
