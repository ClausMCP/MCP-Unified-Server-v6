# plugins/hypothesis_engine/plugin.py
"""
Гипотезы — обёртка над mcp_hypothesis_engine.

ИСПРАВЛЕНО: функции движка синхронные, а плагин делал `await f(...)` → TypeError.
Вызовы переведены на MCPPlugin._acall (sync/async-безопасно).
"""
import json
from typing import List, Dict
from plugins.base_plugin import MCPPlugin

try:
    from mcp_hypothesis_engine import (
        hyp_create_hypothesis, hyp_add_evidence, hyp_list_hypotheses,
        hyp_get_hypothesis, hyp_verify_now, hyp_promote_to_fact,
        hyp_reject_hypothesis, hyp_update_hypothesis_status
    )
    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False
    def hyp_create_hypothesis(*a, **kw): return {"error": "hypothesis not available"}
    def hyp_add_evidence(*a, **kw): return {"error": "hypothesis not available"}
    def hyp_list_hypotheses(*a, **kw): return {"error": "hypothesis not available"}
    def hyp_get_hypothesis(*a, **kw): return {"error": "hypothesis not available"}
    def hyp_verify_now(*a, **kw): return {"error": "hypothesis not available"}
    def hyp_promote_to_fact(*a, **kw): return {"error": "hypothesis not available"}
    def hyp_reject_hypothesis(*a, **kw): return {"error": "hypothesis not available"}
    def hyp_update_hypothesis_status(*a, **kw): return {"error": "hypothesis not available"}


class HypothesisPlugin(MCPPlugin):
    @property
    def name(self) -> str:
        return "Hypothesis Engine"

    @property
    def depends_on(self) -> List[str]:
        return ["Planning Engine", "World Model"]

    async def register_tools(self):
        self.server.add_tool(self.create_hypothesis)
        self.server.add_tool(self.add_evidence)
        self.server.add_tool(self.list_hypotheses)
        self.server.add_tool(self.get_hypothesis)
        self.server.add_tool(self.verify_now)
        self.server.add_tool(self.promote_to_fact)
        self.server.add_tool(self.reject_hypothesis)
        self.server.add_tool(self.update_status)

    async def register_services(self):
        self.provide_service("hypothesis_engine.create", self.create_hypothesis)
        self.provide_service("hypothesis_engine.verify", self.verify_now)

    async def create_hypothesis(self, statement: str, confidence: float = 0.3,
                                explanation: str = "", verification_plan: List[Dict] = None,
                                source_tool: str = "plugin") -> str:
        result = await self._acall(hyp_create_hypothesis, statement, confidence, explanation,
                                   verification_plan or [], source_tool)
        hid = result.get("hypothesis_id") if isinstance(result, dict) else None
        await self.memory_add(f"Гипотеза: {statement[:80]} (уверенность {confidence})",
                              metadata={"type": "hypothesis", "hypothesis_id": hid})
        return json.dumps(result, default=str)

    async def add_evidence(self, hypothesis_id: str, evidence: str,
                           source: str, confidence: float = 0.5) -> str:
        result = await self._acall(hyp_add_evidence, hypothesis_id, evidence, source, confidence)
        await self.memory_add(f"Свидетельство для {hypothesis_id}: {evidence[:80]}",
                              metadata={"type": "evidence"})
        return json.dumps(result, default=str)

    async def list_hypotheses(self, status: str = None) -> str:
        result = await self._acall(hyp_list_hypotheses, status)
        return json.dumps(result, indent=2, default=str)

    async def get_hypothesis(self, hypothesis_id: str) -> str:
        result = await self._acall(hyp_get_hypothesis, hypothesis_id)
        return json.dumps(result, indent=2, default=str)

    async def verify_now(self, hypothesis_id: str) -> str:
        result = await self._acall(hyp_verify_now, hypothesis_id)
        return json.dumps(result, default=str)

    async def promote_to_fact(self, hypothesis_id: str) -> str:
        result = await self._acall(hyp_promote_to_fact, hypothesis_id)
        await self.memory_add(f"Гипотеза {hypothesis_id} продвинута до факта",
                              metadata={"type": "hypothesis_promoted"})
        return json.dumps(result, default=str)

    async def reject_hypothesis(self, hypothesis_id: str, reason: str) -> str:
        result = await self._acall(hyp_reject_hypothesis, hypothesis_id, reason)
        return json.dumps(result, default=str)

    async def update_status(self, hypothesis_id: str, status: str,
                            confidence: float = None, rejection_reason: str = None) -> str:
        result = await self._acall(hyp_update_hypothesis_status, hypothesis_id, status,
                                   confidence, rejection_reason)
        return json.dumps(result, default=str)
