"""使用 Pillow 与 numpy 生成可复现的合成缺陷图像及标注真值。"""

from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

DEFAULT_COUNT = 80
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 240
DEFAULT_DEFECT_RATIO = 0.8

DEFECT_TYPES = (
    {"code": "D01", "name": "外观划痕"},
    {"code": "D02", "name": "尺寸超差"},
    {"code": "D03", "name": "装配不到位"},
    {"code": "D04", "name": "功能失效"},
)


def _make_background(
    width: int,
    height: int,
    rng: np.random.Generator,
) -> Image.Image:
    """生成带轻微渐变和高斯噪声的合成零件表面。"""
    base = rng.integers(78, 190, size=3)
    horizontal = np.linspace(-8, 8, width, dtype=np.float32)
    vertical = np.linspace(-6, 6, height, dtype=np.float32)
    gradient = horizontal[None, :, None] + vertical[:, None, None]
    noise = rng.normal(0.0, 1.8, size=(height, width, 3))
    pixels = base[None, None, :] + gradient + noise
    return Image.fromarray(
        np.clip(pixels, 0, 255).astype(np.uint8),
        mode="RGB",
    )


def _candidate_size(
    defect_code: str,
    rng: random.Random,
) -> tuple[int, int]:
    """按缺陷类别生成具有可区分几何特征的候选框尺寸。"""
    if defect_code == "D01":
        return rng.randint(28, 58), rng.randint(2, 4)
    if defect_code == "D02":
        return rng.randint(14, 24), rng.randint(14, 24)
    if defect_code == "D03":
        return rng.randint(16, 26), rng.randint(16, 26)
    return rng.randint(14, 22), rng.randint(14, 22)


def _overlaps(
    candidate: tuple[int, int, int, int],
    existing: list[tuple[int, int, int, int]],
    padding: int = 7,
) -> bool:
    """判断候选框是否与已有框重叠，避免缺陷粘连后无法区分。"""
    x, y, width, height = candidate
    for other_x, other_y, other_width, other_height in existing:
        if not (
            x + width + padding <= other_x
            or other_x + other_width + padding <= x
            or y + height + padding <= other_y
            or other_y + other_height + padding <= y
        ):
            return True
    return False


def _place_box(
    defect_code: str,
    width: int,
    height: int,
    rng: random.Random,
    existing: list[tuple[int, int, int, int]],
) -> tuple[int, int, int, int] | None:
    """在边界内放置不重叠缺陷框，空间不足时返回 None。"""
    box_width, box_height = _candidate_size(defect_code, rng)
    margin = 4
    for _ in range(120):
        x = rng.randint(margin, width - box_width - margin)
        y = rng.randint(margin, height - box_height - margin)
        candidate = (x, y, box_width, box_height)
        if not _overlaps(candidate, existing):
            return candidate
    return None


def _contrast_color(
    background: tuple[int, int, int],
    direction: int,
) -> tuple[int, int, int]:
    """从背景色出发生成足够明显但仍落在 RGB 范围内的缺陷颜色。"""
    delta = 75 * direction
    return tuple(max(0, min(255, channel + delta)) for channel in background)


def _draw_scratch(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    rng: random.Random,
) -> None:
    """绘制细长折线，模拟外观划痕的几何特征。"""
    x, y, width, height = box
    point_count = max(3, width // 8)
    points = []
    for index in range(point_count):
        point_x = x + round(index * (width - 1) / (point_count - 1))
        point_y = y + rng.randint(0, max(0, height - 1))
        points.append((point_x, point_y))
    draw.line(points, fill=color, width=max(2, height), joint="curve")


def _draw_size_deviation(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
) -> None:
    """绘制实心矩形，模拟尺寸偏差形成的连续高对比区域。"""
    x, y, width, height = box
    draw.rectangle(
        (x, y, x + width - 1, y + height - 1),
        fill=color,
    )


def _draw_misassembly(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
) -> None:
    """绘制缺口圆环，模拟零件装配不到位留下的弧形间隙。"""
    x, y, width, height = box
    draw.arc(
        (x, y, x + width - 1, y + height - 1),
        start=25,
        end=315,
        fill=color,
        width=max(2, min(width, height) // 5),
    )


def _draw_functional_failure(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
) -> None:
    """绘制实心圆盘，模拟指示灯等失效部件的紧凑异常区域。"""
    x, y, width, height = box
    draw.ellipse(
        (x, y, x + width - 1, y + height - 1),
        fill=color,
    )


def _draw_defect(
    image: Image.Image,
    defect_code: str,
    box: tuple[int, int, int, int],
    rng: random.Random,
) -> None:
    """把指定类别的缺陷绘制到合成图上。"""
    x, y, _, _ = box
    background = image.getpixel((x, y))
    color = _contrast_color(background, rng.choice((-1, 1)))
    draw = ImageDraw.Draw(image)

    if defect_code == "D01":
        _draw_scratch(draw, box, color, rng)
    elif defect_code == "D02":
        _draw_size_deviation(draw, box, color)
    elif defect_code == "D03":
        _draw_misassembly(draw, box, color)
    else:
        _draw_functional_failure(draw, box, color)


def _json_bytes(payload: Any) -> bytes:
    """使用稳定排序和换行输出 JSON，确保相同种子字节一致。"""
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def generate_dataset(
    count: int,
    seed: int,
    output_dir: str | Path,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    defect_ratio: float = DEFAULT_DEFECT_RATIO,
) -> dict[str, Any]:
    """生成合成缺陷数据集，并返回不含绝对路径的稳定清单。"""
    if count <= 0:
        raise ValueError("count 必须大于 0")
    if width < 32 or height < 32:
        raise ValueError("width 和 height 必须至少为 32")
    if not 0.0 <= defect_ratio <= 1.0:
        raise ValueError("defect_ratio 必须在 0 到 1 之间")

    output_path = Path(output_dir)
    images_path = output_path / "images"
    labels_path = output_path / "labels"
    images_path.mkdir(parents=True, exist_ok=True)
    labels_path.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    pixel_rng = np.random.default_rng(seed)
    image_names: list[str] = []

    for index in range(count):
        image = _make_background(width, height, pixel_rng)
        image_name = f"image_{index + 1:04d}.png"
        image_names.append(image_name)

        defect_count = 0
        if rng.random() < defect_ratio:
            defect_count = 1
            if index % 5 == 0 and rng.random() < 0.35:
                defect_count = 2

        existing_boxes: list[tuple[int, int, int, int]] = []
        defects: list[dict[str, Any]] = []
        for defect_index in range(defect_count):
            type_index = (index + defect_index) % len(DEFECT_TYPES)
            defect_type = DEFECT_TYPES[type_index]
            box = _place_box(
                defect_type["code"],
                width,
                height,
                rng,
                existing_boxes,
            )
            if box is None:
                continue

            _draw_defect(image, defect_type["code"], box, rng)
            existing_boxes.append(box)
            defects.append(
                {
                    "defect_type_code": defect_type["code"],
                    "defect_type_name": defect_type["name"],
                    "x": box[0],
                    "y": box[1],
                    "w": box[2],
                    "h": box[3],
                }
            )

        image.save(images_path / image_name, format="PNG", compress_level=9)
        label = {
            "image_file": image_name,
            "width": width,
            "height": height,
            "is_synthetic": True,
            "seed": seed,
            "defects": defects,
        }
        (labels_path / f"{image_name}.json").write_bytes(_json_bytes(label))

    manifest = {
        "count": count,
        "seed": seed,
        "width": width,
        "height": height,
        "defect_ratio": defect_ratio,
        "is_synthetic": True,
        "images": image_names,
    }
    (output_path / "manifest.json").write_bytes(_json_bytes(manifest))
    return manifest


def register_dataset(
    dataset_dir: str | Path,
    database_path: str | Path,
) -> dict[str, int]:
    """把生成的图片元数据登记到指定数据库，不写入任何标注框。"""
    dataset_path = Path(dataset_dir).resolve()
    resolved_database = Path(database_path).resolve()
    os.environ["QUALITY_DATABASE_PATH"] = str(resolved_database)

    from app import app
    from models import DefectImage, DefectType, db

    manifest = json.loads(
        (dataset_path / "manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("is_synthetic") is not True:
        raise ValueError("数据集未明确标记为合成数据")

    with app.app_context():
        existing_codes = {
            code for (code,) in db.session.query(DefectType.code).all()
        }
        for defect_type in DEFECT_TYPES:
            if defect_type["code"] not in existing_codes:
                db.session.add(
                    DefectType(
                        code=defect_type["code"],
                        name=defect_type["name"],
                        description="程序合成缺陷类型，非真实工业样本",
                    )
                )

        existing_files = {
            file_name
            for (file_name,) in db.session.query(DefectImage.file_name).all()
        }
        registered = 0
        skipped = 0
        for image_name in manifest["images"]:
            label = json.loads(
                (dataset_path / "labels" / f"{image_name}.json").read_text(
                    encoding="utf-8"
                )
            )
            if label.get("is_synthetic") is not True:
                raise ValueError(f"标签未标记为合成数据：{image_name}")
            if image_name in existing_files:
                skipped += 1
                continue
            db.session.add(
                DefectImage(
                    file_name=image_name,
                    width=int(label["width"]),
                    height=int(label["height"]),
                    label_status="未标注",
                    is_synthetic=True,
                )
            )
            registered += 1

        db.session.commit()
    return {"registered": registered, "skipped": skipped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "生成可复现的合成缺陷图像数据集；"
            "数据仅用于流程与算法验证，不代表真实工业缺陷分布。"
        )
    )
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data") / "defect_synth",
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument(
        "--defect-ratio",
        type=float,
        default=DEFAULT_DEFECT_RATIO,
    )
    parser.add_argument(
        "--database",
        type=Path,
        help=(
            "可选：把生成图片登记到指定 SQLite；"
            "不传时只生成文件，不修改任何数据库。"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = generate_dataset(
        count=args.count,
        seed=args.seed,
        output_dir=args.out,
        width=args.width,
        height=args.height,
        defect_ratio=args.defect_ratio,
    )
    defect_total = sum(
        1
        for image_name in manifest["images"]
        for _ in json.loads(
            (
                args.out / "labels" / f"{image_name}.json"
            ).read_text(encoding="utf-8")
        )["defects"]
    )
    registration = None
    if args.database is not None:
        registration = register_dataset(args.out, args.database)

    print(
        f"已生成合成数据：{manifest['count']} 张图像，"
        f"{defect_total} 个缺陷框，seed={manifest['seed']}，"
        f"输出目录={args.out}"
    )
    if registration is not None:
        print(
            f"数据库登记：registered={registration['registered']}，"
            f"skipped={registration['skipped']}，database={args.database}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
