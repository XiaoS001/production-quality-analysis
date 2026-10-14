"""AI 助手测试：工具查真实数据、接口鉴权、模型失败降级。

全程不消耗真实 API：需要大模型的用例都用 monkeypatch 替换 ``assistant.call_llm``。
"""

from datetime import datetime, timedelta

import assistant
from assistant_tools import execute_tool
from models import (
    Batch,
    DefectAnnotation,
    DefectImage,
    DefectType,
    InspectionItem,
    InspectionRecord,
    db,
)


def seed_inspection(
    app,
    seeded_data,
    *,
    inspected_qty=200,
    passed_qty=190,
    failed_qty=10,
    defect_code="D01",
    days_ago=0,
):
    """写入一条真实检验记录，供工具查询。"""
    with app.app_context():
        batch = (
            db.session.query(Batch)
            .filter_by(batch_no=seeded_data["batch_1_no"])
            .one()
        )
        item = db.session.query(InspectionItem).first()
        defect = (
            db.session.query(DefectType)
            .filter_by(code=defect_code)
            .one()
        )
        record = InspectionRecord(
            batch_id=batch.id,
            item_id=item.id,
            inspected_qty=inspected_qty,
            passed_qty=passed_qty,
            failed_qty=failed_qty,
            defect_type_id=defect.id,
            inspected_at=datetime.now() - timedelta(days=days_ago),
        )
        db.session.add(record)
        db.session.commit()
        return record.id


def make_fake_llm(tool_name="query_overall_yield"):
    """构造一个大模型替身：先返回工具调用，再返回自然语言回答。"""
    calls: list[dict] = []

    def fake_call_llm(messages, tools=None):
        calls.append({"has_tools": bool(tools), "messages": messages})
        if tools:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": "{}"},
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": "依据数据库直查结果：整体良率 95.0%，不合格 10 件。",
        }

    return fake_call_llm, calls


# --------------------------------------------------------------------------- #
# 工具层
# --------------------------------------------------------------------------- #
def test_overall_yield_tool_counts_real_records(app, seeded_data):
    seed_inspection(app, seeded_data)

    with app.app_context():
        result = execute_tool("query_overall_yield", {})

    assert "error" not in result
    assert result["tool"] == "query_overall_yield"
    row = result["data"][0]
    assert row["inspected_qty"] == 200
    assert row["passed_qty"] == 190
    assert row["overall_yield_rate"] == 95.0
    assert "95.0%" in result["summary"]


def test_batch_tool_filters_by_batch_no(app, seeded_data):
    seed_inspection(app, seeded_data)

    with app.app_context():
        hit = execute_tool("query_yield_by_batch", {"batch_no": "B001"})
        miss = execute_tool("query_yield_by_batch", {"batch_no": "B999"})

    assert hit["row_count"] == 1
    assert hit["data"][0]["batch_no"] == "B001"
    assert miss["row_count"] == 0


def test_defect_distribution_tool_computes_ratio(app, seeded_data):
    seed_inspection(app, seeded_data, failed_qty=10, defect_code="D01")
    seed_inspection(app, seeded_data, failed_qty=30, defect_code="D02")

    with app.app_context():
        result = execute_tool("query_defect_distribution", {})

    top = result["data"][0]
    assert top["defect_name"] == "尺寸超差"
    assert top["count"] == 30
    assert top["ratio"] == 75.0


def test_annotation_progress_tool(app, seeded_data):
    with app.app_context():
        defect = db.session.query(DefectType).filter_by(code="D01").one()
        image = DefectImage(
            file_name="image_0001.png",
            width=100,
            height=80,
            label_status="已标注",
            is_synthetic=True,
        )
        db.session.add(image)
        db.session.flush()
        db.session.add(
            DefectAnnotation(
                image_id=image.id,
                defect_type_id=defect.id,
                x=1,
                y=2,
                w=3,
                h=4,
            )
        )
        db.session.commit()

    with app.app_context():
        result = execute_tool("query_annotation_progress", {})

    row = result["data"][0]
    assert row["total_images"] == 1
    assert row["已标注"] == 1
    assert row["annotation_count"] == 1


def test_unknown_tool_returns_structured_error(app):
    with app.app_context():
        result = execute_tool("query_not_exists", {})

    assert result["error"] == "unknown_tool"
    assert result["row_count"] == 0


def test_tool_bad_parameters_are_caught(app):
    with app.app_context():
        result = execute_tool(
            "query_yield_by_batch", {"unexpected_param": 1}
        )

    assert result["error"] == "bad_parameters"


# --------------------------------------------------------------------------- #
# 接口层：鉴权
# --------------------------------------------------------------------------- #
def test_ask_requires_token(anon_client):
    response = anon_client.post(
        "/api/assistant/ask", json={"question": "整体良率多少？"}
    )

    assert response.status_code == 401


def test_ask_rejects_empty_question(client):
    response = client.post("/api/assistant/ask", json={"question": "   "})

    assert response.status_code == 400
    assert response.get_json() == {"error": "question 不能为空"}


def test_ask_rejects_overlong_question(client):
    response = client.post(
        "/api/assistant/ask", json={"question": "良" * 501}
    )

    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 接口层：离线模式（无密钥）
# --------------------------------------------------------------------------- #
def test_ask_offline_uses_real_data(app, client, seeded_data, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    seed_inspection(app, seeded_data)

    response = client.post(
        "/api/assistant/ask", json={"question": "批次 B001 的良率是多少？"}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["mode"] == "offline"
    assert body["tool_calls"][0]["tool"] == "query_yield_by_batch"
    assert "95.0%" in body["answer"]
    assert body["asked_by"] == "admin"


def test_viewer_can_ask_assistant(viewer_client, seeded_data, monkeypatch):
    """只读角色也有查看权限，因此可以提问。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    response = viewer_client.post(
        "/api/assistant/ask", json={"question": "当前有多少未关闭异常？"}
    )

    assert response.status_code == 200
    assert response.get_json()["role"] == "viewer"


# --------------------------------------------------------------------------- #
# 接口层：大模型模式与降级
# --------------------------------------------------------------------------- #
def test_ask_llm_mode_calls_tools_then_answers(
    client, seeded_data, monkeypatch
):
    fake, calls = make_fake_llm()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(assistant, "call_llm", fake)

    response = client.post(
        "/api/assistant/ask", json={"question": "帮我看看整体质量情况"}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["mode"] == "llm"
    assert body["answer"] == "依据数据库直查结果：整体良率 95.0%，不合格 10 件。"
    # 同一个工具不会被重复执行
    assert len(body["tool_calls"]) == 1
    assert body["tool_calls"][0]["tool"] == "query_overall_yield"
    # 第一轮带工具让模型选工具，最后一轮不带工具让它作答
    assert calls[0]["has_tools"] is True
    assert calls[-1]["has_tools"] is False


def test_ask_falls_back_when_llm_fails(client, seeded_data, monkeypatch):
    def broken_call_llm(messages, tools=None):
        raise TimeoutError("模型接口超时")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(assistant, "call_llm", broken_call_llm)

    response = client.post(
        "/api/assistant/ask", json={"question": "整体良率多少？"}
    )

    # 关键：模型挂了也必须是 200 + 有答案，而不是 500
    assert response.status_code == 200
    body = response.get_json()
    assert body["mode"] == "offline_fallback"
    assert body["notice"] and "降级" in body["notice"]
    assert body["answer"]


def test_ask_falls_back_when_answer_stage_fails(
    client, seeded_data, monkeypatch
):
    """选工具成功、生成回答失败 —— 仍要给出可用答案。"""
    state = {"round": 0}

    def partly_broken(messages, tools=None):
        state["round"] += 1
        if tools:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "query_overall_yield",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        raise RuntimeError("回答阶段失败")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(assistant, "call_llm", partly_broken)

    response = client.post(
        "/api/assistant/ask", json={"question": "整体良率多少？"}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["mode"] == "offline_fallback"
    assert body["answer"]


def test_offline_plan_picks_expected_tools():
    assert assistant.offline_plan("不良分布怎么样")[0]["name"] == (
        "query_defect_distribution"
    )
    assert assistant.offline_plan("标注进度如何")[0]["name"] == (
        "query_annotation_progress"
    )
    assert assistant.offline_plan("随便问问")[0]["name"] == (
        "query_overall_yield"
    )
