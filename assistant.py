"""AI 助手：用 LangGraph 编排「选工具 -> 查真实数据 -> 生成回答」的循环。

三种运行模式
------------
- ``llm``              —— 配置了 ``DEEPSEEK_API_KEY``，走大模型 function calling；
- ``offline``          —— 未配置密钥：本地关键词路由 + 模板化回答（零 API 消耗，可测试）；
- ``offline_fallback`` —— 大模型调用超时/报错时**自动降级**为离线回答，接口仍返回 200。

图结构（LangGraph ``StateGraph``）::

    decide --(仍需取数)--> act --+
      |                            |
      +--(已够/已达上限)--> answer <+

要点：所有数字都来自 ``assistant_tools`` 对**真实数据库**的查询，
助手不编造数据；模型只负责「选工具」和「把查询结果讲成人话」。
"""

from __future__ import annotations

import json
import os
from typing import Any, TypedDict

import requests
from flask import Blueprint, g, jsonify, request
from langgraph.graph import END, StateGraph

from assistant_tools import TOOL_SPECS, execute_tool
from auth import PERM_VIEW, require_permission

MAX_STEPS = 2

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 20.0

SYSTEM_PROMPT = (
    "你是生产质量数据分析助手。只能依据工具返回的数据回答，"
    "不允许编造数字；回答要简短、带具体数字和单位；"
    "若数据为空就直接说明“当前没有相关记录”。"
)

assistant_bp = Blueprint("assistant", __name__)


class AssistantState(TypedDict, total=False):
    question: str
    mode: str
    steps: int
    planned: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    answer: str
    notice: str


# --------------------------------------------------------------------------- #
# 大模型调用（DeepSeek，OpenAI 兼容）
# --------------------------------------------------------------------------- #
def llm_available() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def _api_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _base_url() -> str:
    return os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def call_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """调用大模型；失败会抛异常，由调用方负责降级。"""
    payload: dict[str, Any] = {
        "model": os.environ.get("XM02_LLM_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "temperature": 0,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    response = requests.post(
        f"{_base_url()}/chat/completions",
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=float(os.environ.get("XM02_LLM_TIMEOUT", DEFAULT_TIMEOUT)),
    )
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("大模型返回内容为空")
    return choices[0]["message"]


# --------------------------------------------------------------------------- #
# 规划（decide）
# --------------------------------------------------------------------------- #
OFFLINE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("query_annotation_progress", ("标注", "复核")),
    ("query_defect_distribution", ("不良", "缺陷", "分布", "占比")),
    ("query_quality_issues", ("异常", "待处理", "处理中", "已关闭", "问题")),
    ("query_yield_trend", ("趋势", "走势", "最近几天")),
    ("query_recent_inspections", ("检验记录", "明细", "记录")),
    ("query_yield_by_product", ("产品",)),
    ("query_yield_by_batch", ("批次", "批号")),
)


def offline_plan(question: str) -> list[dict[str, Any]]:
    """离线模式：关键词路由，最多选两个工具。"""
    text = question or ""
    picked: list[dict[str, Any]] = []
    for name, keywords in OFFLINE_RULES:
        if any(keyword in text for keyword in keywords):
            picked.append({"name": name, "arguments": {}})
        if len(picked) >= 2:
            break
    if not picked:
        picked.append({"name": "query_overall_yield", "arguments": {}})
    return picked


def _parse_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls") or []
    planned: list[dict[str, Any]] = []
    for call in calls:
        function = call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            arguments = {}
        planned.append(
            {"name": name, "arguments": arguments if isinstance(arguments, dict) else {}}
        )
    return planned


def _decide(state: AssistantState) -> AssistantState:
    question = state.get("question", "")
    mode = state.get("mode", "offline")
    results = state.get("tool_results") or []
    already_has_results = bool(results)
    executed = {item.get("tool") for item in results}

    if mode == "llm":
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        if results:
            summaries = json.dumps(
                [
                    {"tool": item.get("tool"), "summary": item.get("summary")}
                    for item in results
                ],
                ensure_ascii=False,
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"已查询到的数据摘要：{summaries}\n"
                        f"已用过的工具：{sorted(name for name in executed if name)}\n"
                        "如果还需要其他数据就继续调用工具（不要重复调用已用过的工具），"
                        "否则不再调用工具。"
                    ),
                }
            )
        try:
            message = call_llm(messages, tools=TOOL_SPECS)
        except Exception as exc:  # noqa: BLE001 - 触发降级
            return {
                "mode": "offline_fallback",
                "planned": (
                    [] if already_has_results else offline_plan(question)
                ),
                "notice": f"大模型调用失败，已降级为本地数据直查：{exc}",
            }
        # 过滤掉已经执行过的工具，避免同一轮反复查同一条数据
        planned = [
            item
            for item in _parse_tool_calls(message)
            if item.get("name") not in executed
        ]
        if not planned and not already_has_results:
            planned = [{"name": "query_overall_yield", "arguments": {}}]
        return {"planned": planned}

    return {
        "planned": [] if already_has_results else offline_plan(question)
    }


def _act(state: AssistantState) -> AssistantState:
    results = list(state.get("tool_results") or [])
    for item in state.get("planned") or []:
        results.append(execute_tool(item["name"], item.get("arguments")))
    return {
        "tool_results": results,
        "planned": [],
        "steps": int(state.get("steps") or 0) + 1,
    }


def _route_after_decide(state: AssistantState) -> str:
    if state.get("planned") and int(state.get("steps") or 0) < MAX_STEPS:
        return "act"
    return "answer"


def _answer(state: AssistantState) -> AssistantState:
    question = state.get("question", "")
    results = state.get("tool_results") or []
    mode = state.get("mode", "offline")

    if not results:
        return {
            "answer": "没有查询到可用于回答的数据，请换一个问法或先录入检验记录。",
            "notice": state.get("notice"),
        }

    if mode == "llm":
        context = json.dumps(
            [
                {
                    "tool": item.get("tool"),
                    "summary": item.get("summary"),
                    "data": item.get("data"),
                }
                for item in results
            ],
            ensure_ascii=False,
        )
        try:
            message = call_llm(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"问题：{question}\n\n工具查询结果（JSON）：{context}",
                    },
                ]
            )
            content = (message.get("content") or "").strip()
            if content:
                return {"answer": content}
        except Exception as exc:  # noqa: BLE001 - 降级为模板回答
            mode = "offline_fallback"
        else:
            mode = "offline_fallback"
        return {
            "mode": mode,
            "answer": _offline_answer(question, results),
            "notice": state.get("notice")
            or "大模型生成回答失败，已降级为本地数据摘要。",
        }

    return {
        "answer": _offline_answer(question, results),
        "notice": state.get("notice"),
    }


def _offline_answer(question: str, results: list[dict[str, Any]]) -> str:
    parts = [
        item.get("summary", "")
        for item in results
        if item.get("summary")
    ]
    prefix = "（离线模式：以下为数据库直查结果）"
    return prefix + " ".join(parts) if parts else f"{prefix}没有查到与「{question}」相关的数据。"


# --------------------------------------------------------------------------- #
# 图构建
# --------------------------------------------------------------------------- #
_GRAPH: Any = None


def build_graph():
    """构建并编译 LangGraph 状态图（模块级缓存）。"""
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH

    graph = StateGraph(AssistantState)
    graph.add_node("decide", _decide)
    graph.add_node("act", _act)
    graph.add_node("answer", _answer)

    graph.set_entry_point("decide")
    graph.add_conditional_edges(
        "decide",
        _route_after_decide,
        {"act": "act", "answer": "answer"},
    )
    graph.add_edge("act", "decide")
    graph.add_edge("answer", END)

    _GRAPH = graph.compile()
    return _GRAPH


def ask(question: str) -> dict[str, Any]:
    """对外入口：提问 -> 结构化回答。任何异常都在这里兜底，保证不抛 500。"""
    mode = "llm" if llm_available() else "offline"
    initial: AssistantState = {
        "question": question,
        "mode": mode,
        "steps": 0,
        "planned": [],
        "tool_results": [],
    }
    try:
        final = build_graph().invoke(initial)
    except Exception as exc:  # noqa: BLE001 - 图执行异常也要降级
        return {
            "answer": _offline_answer(question, []),
            "mode": "offline_fallback",
            "notice": f"助手执行异常，已降级：{exc}",
            "tool_calls": [],
            "data": [],
        }

    results = final.get("tool_results") or []
    return {
        "answer": final.get("answer") or _offline_answer(question, results),
        "mode": final.get("mode", mode),
        "notice": final.get("notice"),
        "tool_calls": [
            {
                "tool": item.get("tool"),
                "params": item.get("params"),
                "row_count": item.get("row_count"),
                "error": item.get("error"),
            }
            for item in results
        ],
        "data": results,
    }


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@assistant_bp.post("/api/assistant/ask")
@require_permission(PERM_VIEW)
def ask_endpoint():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return jsonify({"error": "question 不能为空"}), 400
    if len(question) > 500:
        return jsonify({"error": "question 过长（上限 500 字符）"}), 400

    result = ask(question.strip())
    result["asked_by"] = g.current_identity["username"]
    result["role"] = g.current_identity["role"]
    return jsonify(result)
