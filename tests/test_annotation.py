"""缺陷图像标注流程接口测试。"""

from models import DefectImage, DefectType, db


def seed_annotation_image(app):
    with app.app_context():
        defect_type = DefectType(
            code="D01",
            name="外观划痕",
            description="合成测试缺陷",
        )
        image = DefectImage(
            file_name="image_0001.png",
            width=100,
            height=80,
            is_synthetic=True,
        )
        db.session.add_all([defect_type, image])
        db.session.commit()
        return image.id, defect_type.id


def test_annotation_stats_summarize_progress(client, app):
    with app.app_context():
        defect_type = DefectType(code="D01", name="外观划痕")
        images = [
            DefectImage(
                file_name=f"image_{index:04d}.png",
                width=100,
                height=80,
                is_synthetic=True,
            )
            for index in range(1, 4)
        ]
        db.session.add_all([defect_type, *images])
        db.session.commit()
        defect_type_id = defect_type.id
        image_ids = [image.id for image in images]

    client.post(
        f"/api/annotation/image/{image_ids[0]}",
        json={
            "labeled_by": "张三",
            "annotations": [
                {
                    "defect_type_id": defect_type_id,
                    "x": 5,
                    "y": 5,
                    "w": 20,
                    "h": 10,
                }
            ],
        },
    )
    client.post(
        f"/api/annotation/image/{image_ids[1]}",
        json={"labeled_by": "张三", "annotations": []},
    )
    client.post(
        f"/api/annotation/image/{image_ids[1]}/review",
        json={"reviewed_by": "李四"},
    )

    response = client.get("/api/annotation/stats")

    assert response.status_code == 200
    assert response.get_json() == {
        "is_synthetic": True,
        "total": 3,
        "labeled": 1,
        "reviewed": 1,
        "unlabeled": 1,
        "defect_distribution": [
            {
                "defect_type_code": "D01",
                "defect_type_name": "外观划痕",
                "count": 1,
            }
        ],
    }


def test_annotation_create_query_update_and_review(client, app):
    image_id, defect_type_id = seed_annotation_image(app)

    initial = client.get(f"/api/annotation/image/{image_id}")
    assert initial.status_code == 200
    assert initial.get_json()["label_status"] == "未标注"
    assert initial.get_json()["annotations"] == []

    created = client.post(
        f"/api/annotation/image/{image_id}",
        json={
            "labeled_by": "张三",
            "annotations": [
                {
                    "defect_type_id": defect_type_id,
                    "x": 10,
                    "y": 12,
                    "w": 20,
                    "h": 8,
                },
                {
                    "defect_type_id": defect_type_id,
                    "x": 50,
                    "y": 40,
                    "w": 18,
                    "h": 10,
                },
            ],
        },
    )
    assert created.status_code == 200
    assert created.get_json()["label_status"] == "已标注"
    assert created.get_json()["labeled_by"] == "张三"
    assert created.get_json()["labeled_at"] is not None
    assert len(created.get_json()["annotations"]) == 2
    assert all(
        item["defect_type_code"] == "D01"
        for item in created.get_json()["annotations"]
    )

    queried = client.get(f"/api/annotation/image/{image_id}")
    assert queried.status_code == 200
    assert len(queried.get_json()["annotations"]) == 2

    updated = client.post(
        f"/api/annotation/image/{image_id}",
        json={
            "labeled_by": "张三",
            "annotations": [
                {
                    "defect_type_id": defect_type_id,
                    "x": 11,
                    "y": 13,
                    "w": 21,
                    "h": 9,
                }
            ],
        },
    )
    assert updated.status_code == 200
    assert len(updated.get_json()["annotations"]) == 1
    assert updated.get_json()["annotations"][0]["x"] == 11

    reviewed = client.post(
        f"/api/annotation/image/{image_id}/review",
        json={"reviewed_by": "李四"},
    )
    assert reviewed.status_code == 200
    assert reviewed.get_json()["label_status"] == "已复核"
    assert reviewed.get_json()["reviewed_by"] == "李四"
    assert reviewed.get_json()["reviewed_at"] is not None


def test_reviewed_annotation_is_immutable(client, app):
    image_id, defect_type_id = seed_annotation_image(app)
    original_payload = {
        "labeled_by": "张三",
        "annotations": [
            {
                "defect_type_id": defect_type_id,
                "x": 10,
                "y": 12,
                "w": 20,
                "h": 8,
            }
        ],
    }
    client.post(
        f"/api/annotation/image/{image_id}",
        json=original_payload,
    )
    client.post(
        f"/api/annotation/image/{image_id}/review",
        json={"reviewed_by": "李四"},
    )

    update_after_review = client.post(
        f"/api/annotation/image/{image_id}",
        json={
            "labeled_by": "王五",
            "annotations": [],
        },
    )
    review_again = client.post(
        f"/api/annotation/image/{image_id}/review",
        json={"reviewed_by": "王五"},
    )
    current = client.get(
        f"/api/annotation/image/{image_id}"
    ).get_json()

    assert update_after_review.status_code == 409
    assert review_again.status_code == 409
    assert current["label_status"] == "已复核"
    assert current["reviewed_by"] == "李四"
    assert len(current["annotations"]) == 1
    assert current["annotations"][0]["x"] == 10
