from __future__ import annotations

import unittest
from unittest.mock import patch

from app.graph import graph_service
from app.schemas import ChatMessage
from app.services.intent_service import IntentDecision


def decision(intent: str, missing_slots: list[str] | None = None) -> IntentDecision:
    return IntentDecision(
        intent=intent,  # type: ignore[arg-type]
        confidence=1.0,
        slots={
            "language": "python",
            "task": "do the thing",
            "function_name": "target",
            "parameters": "value",
            "constraints": None,
            "search_symbols": "target",
        },
        missing_slots=missing_slots or [],
        follow_up_question="Need more detail." if missing_slots else None,
    )


class GraphServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        graph_service._GRAPH = None

    def tearDown(self) -> None:
        graph_service._GRAPH = None

    def test_routes_general_chat_to_chatbot_node(self) -> None:
        calls: list[str] = []

        def intent_node(state):
            calls.append("intent")
            return {"decision": decision("general_chat")}

        def chatbot_node(state):
            calls.append("chatbot")
            return {"content": "hello", "executed": False}

        with patch.object(graph_service, "intent_node", intent_node), patch.object(
            graph_service, "chatbot_node", chatbot_node
        ), patch.object(graph_service, "_intent_routing_available", return_value=True):
            result = graph_service.handle_chat([ChatMessage(role="user", content="hi")])

        self.assertEqual(result.content, "hello")
        self.assertFalse(result.executed)
        self.assertEqual(calls, ["intent", "chatbot"])

    def test_routes_missing_slots_to_follow_up_node(self) -> None:
        calls: list[str] = []

        def intent_node(state):
            calls.append("intent")
            return {"decision": decision("create_function", ["task"])}

        def follow_up_node(state):
            calls.append("follow_up")
            return {"content": "Need more detail.", "executed": False}

        with patch.object(graph_service, "intent_node", intent_node), patch.object(
            graph_service, "follow_up_node", follow_up_node
        ), patch.object(graph_service, "_intent_routing_available", return_value=True):
            result = graph_service.handle_chat([ChatMessage(role="user", content="add function")])

        self.assertEqual(result.content, "Need more detail.")
        self.assertFalse(result.executed)
        self.assertEqual(calls, ["intent", "follow_up"])

    def test_routes_ready_code_task_through_code_chain(self) -> None:
        calls: list[str] = []

        def intent_node(state):
            calls.append("intent")
            return {"decision": decision("modify_function")}

        def planner_node(state):
            calls.append("planner")
            return {"planner_messages": state["messages"]}

        def coder_node(state):
            calls.append("coder")
            return {"raw_code": "def target(value):\n    return value\n"}

        def review_node(state):
            calls.append("review")
            return {"content": "```python\ndef target(value):\n    return value\n```", "executed": True}

        def patch_node(state):
            calls.append("patch")
            return {"patch": None}

        with patch.object(graph_service, "intent_node", intent_node), patch.object(
            graph_service, "planner_node", planner_node
        ), patch.object(graph_service, "coder_node", coder_node), patch.object(
            graph_service, "review_node", review_node
        ), patch.object(graph_service, "patch_node", patch_node), patch.object(
            graph_service, "_intent_routing_available", return_value=True
        ):
            result = graph_service.handle_chat([ChatMessage(role="user", content="change target")])

        self.assertTrue(result.executed)
        self.assertIn("def target", result.content)
        self.assertEqual(calls, ["intent", "planner", "coder", "review", "patch"])


if __name__ == "__main__":
    unittest.main()
