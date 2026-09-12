"""
多 agent 编排级 orchestrator。

把主路由、执行路由和 handoff 组合成统一编排计划，
为主回复链、统一 RAG 链和长期演进治理提供一致的 orchestrator 视图。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.common.types import RoutingDecision


@dataclass
class AgentRegistration:
    name: str
    role: str
    capabilities: List[str] = field(default_factory=list)
    fallback_agents: List[str] = field(default_factory=list)
    execute: Optional[Callable] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "capabilities": list(self.capabilities),
            "fallback_agents": list(self.fallback_agents),
            "metadata": dict(self.metadata),
        }


class AgentOrchestrator:
    """统一生成 reply/rag 多 agent 编排计划。"""

    def __init__(self):
        self._registry: Dict[str, AgentRegistration] = {}
        self._register_defaults()

    def _execute_rag_reply(self, service, message, context, intent_result, **kwargs):
        try:
            result = service._standard_analysis(
                message=message,
                customer_name=context.get("customer_name", ""),
                conversation_history=context.get("conversation_history", []),
                customer_data=context.get("customer_data", {}),
                base_result=context.get("base_result", {}),
                intent_result=intent_result,
                retrieval_query=context.get("retrieval_query"),
                session_id=context.get("session_id"),
                rag_need_retrieval=True,
                generation_mode=kwargs.get("generation_mode", "sales"),
            )
            return {"success": True, "reply": result, "agent": "retrieval_augmented_agent"}
        except Exception as e:
            return {"success": False, "error": str(e), "agent": "retrieval_augmented_agent"}

    def _execute_llm_generation(self, service, message, context, intent_result, **kwargs):
        try:
            result = service._generate_llm_only_fallback_reply(
                mode=kwargs.get("mode", "sales"),
                message=message,
                conversation_history=context.get("conversation_history"),
                reply_analysis=context.get("reply_analysis"),
            )
            if result is None:
                return {"success": False, "error": "llm_generation returned None", "agent": "llm_generation_agent"}
            return {"success": True, "reply": result, "agent": "llm_generation_agent"}
        except Exception as e:
            return {"success": False, "error": str(e), "agent": "llm_generation_agent"}

    def _execute_human_handoff(self, service, message, context, intent_result, **kwargs):
        return {"success": True, "need_human": True, "agent": "human_handoff_agent"}

    def _execute_direct_reply(self, service, message, context, intent_result, **kwargs):
        try:
            result = service._generate_base_reply(
                message=message,
                customer_name=context.get("customer_name", ""),
                intent_result=intent_result,
                conversation_history=context.get("conversation_history", []),
                customer_data=context.get("customer_data", {}),
            )
            if not result:
                return {"success": False, "error": "direct_reply returned empty", "agent": "direct_reply_agent"}
            return {"success": True, "reply": result, "agent": "direct_reply_agent"}
        except Exception as e:
            return {"success": False, "error": str(e), "agent": "direct_reply_agent"}

    def _execute_fallback_generation(self, service, message, context, intent_result, **kwargs):
        try:
            result = service._generate_llm_only_fallback_reply(
                mode=kwargs.get("mode", "fallback"),
                message=message,
                conversation_history=context.get("conversation_history"),
                reply_analysis=context.get("reply_analysis"),
            )
            if result is None:
                return {"success": False, "error": "fallback_generation returned None", "agent": "fallback_generation_agent"}
            return {"success": True, "reply": result, "agent": "fallback_generation_agent"}
        except Exception as e:
            return {"success": False, "error": str(e), "agent": "fallback_generation_agent"}

    def _register_defaults(self):
        self.register_agent(
            AgentRegistration(
                name="main_router_agent",
                role="planner",
                capabilities=["intent_route", "reply_route_selection"],
            )
        )
        self.register_agent(
            AgentRegistration(
                name="rag_pipeline_router_agent",
                role="planner",
                capabilities=["rag_upgrade_route", "rag_execution_route"],
            )
        )
        self.register_agent(
            AgentRegistration(
                name="base_reply_agent",
                role="executor",
                capabilities=["base_reply"],
                fallback_agents=["fallback_generation_agent"],
                execute=self._execute_direct_reply,
            )
        )
        self.register_agent(
            AgentRegistration(
                name="retrieval_augmented_agent",
                role="executor",
                capabilities=["knowledge_retrieval", "evidence_grounded_reply"],
                fallback_agents=["base_reply_agent"],
                execute=self._execute_rag_reply,
            )
        )
        self.register_agent(
            AgentRegistration(
                name="llm_generation_agent",
                role="executor",
                capabilities=["rag_llm_generation", "fallback_generation"],
                fallback_agents=["fallback_generation_agent"],
                execute=self._execute_llm_generation,
            )
        )
        self.register_agent(
            AgentRegistration(
                name="human_handoff_agent",
                role="handoff",
                capabilities=["human_review", "manual_followup"],
                fallback_agents=["llm_generation_agent"],
                execute=self._execute_human_handoff,
            )
        )
        self.register_agent(
            AgentRegistration(
                name="direct_reply_agent",
                role="executor",
                capabilities=["no_retrieval_reply"],
                fallback_agents=["modular_rag_agent"],
                execute=self._execute_direct_reply,
            )
        )
        self.register_agent(
            AgentRegistration(
                name="modular_rag_agent",
                role="executor",
                capabilities=["retrieval", "modular_rag"],
                fallback_agents=["fallback_generation_agent"],
            )
        )
        self.register_agent(
            AgentRegistration(
                name="agentic_rag_agent",
                role="executor",
                capabilities=["multi_tool_reasoning", "knowledge_graph", "agentic_rag"],
                fallback_agents=["modular_rag_agent"],
            )
        )
        self.register_agent(
            AgentRegistration(
                name="fallback_generation_agent",
                role="executor",
                capabilities=["fallback_generation"],
                fallback_agents=[],
                execute=self._execute_fallback_generation,
            )
        )

    def register_agent(self, registration: AgentRegistration):
        self._registry[registration.name] = registration

    def execute_chain(self, plan, service, message, context, intent_result, **kwargs):
        results = []
        for step in plan.get("steps", []):
            agent_name = step.get("agent")
            agent = self._registry.get(agent_name)
            if not agent or not agent.execute:
                continue
            result = agent.execute(service, message, context, intent_result, **kwargs)
            results.append(result)
            if result.get("success"):
                return result
            for fallback_name in agent.fallback_agents:
                fallback = self._registry.get(fallback_name)
                if fallback and fallback.execute:
                    fb_result = fallback.execute(service, message, context, intent_result, **kwargs)
                    results.append(fb_result)
                    if fb_result.get("success"):
                        return fb_result
        return results[-1] if results else {"success": False, "error": "no agent executed"}

    def _build_agent_entry(self, agent_name: str, *, selected: bool, decision: Optional[RoutingDecision] = None) -> Dict[str, Any]:
        registration = self._registry.get(agent_name)
        payload = registration.to_dict() if registration else {
            "name": agent_name,
            "role": "executor",
            "capabilities": [],
            "fallback_agents": [],
            "metadata": {},
        }
        payload["selected"] = selected
        if decision is not None:
            payload["decision"] = decision.to_dict()
        return payload

    def plan_reply_chain(
        self,
        *,
        main_decision: RoutingDecision,
        execution_decision: RoutingDecision,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        handoffs: List[Dict[str, Any]] = []
        if execution_decision.handoff_reason:
            handoffs.append(
                {
                    "from_agent": "main_router_agent",
                    "to_agent": execution_decision.route_name,
                    "reason": execution_decision.handoff_reason,
                    "fallback_route": execution_decision.fallback_route,
                }
            )

        return {
            "orchestrator": "reply_orchestrator_v1",
            "mode": "reply_chain",
            "final_agent": execution_decision.route_name,
            "final_executor": execution_decision.executor,
            "agents": [
                self._build_agent_entry("main_router_agent", selected=True, decision=main_decision),
                self._build_agent_entry(execution_decision.route_name, selected=True, decision=execution_decision),
            ],
            "handoffs": handoffs,
            "metadata": {
                "main_route": main_decision.route_name,
                "execution_route": execution_decision.route_name,
                **dict(metadata or {}),
            },
        }

    def plan_rag_chain(
        self,
        *,
        pipeline_decision: RoutingDecision,
        execution_decision: RoutingDecision,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        handoffs: List[Dict[str, Any]] = []
        if execution_decision.handoff_reason:
            handoffs.append(
                {
                    "from_agent": pipeline_decision.route_name,
                    "to_agent": execution_decision.route_name,
                    "reason": execution_decision.handoff_reason,
                    "fallback_route": execution_decision.fallback_route,
                }
            )

        planner_agent = "rag_pipeline_router_agent"
        return {
            "orchestrator": "rag_orchestrator_v1",
            "mode": "rag_pipeline",
            "final_agent": execution_decision.route_name,
            "final_executor": execution_decision.executor,
            "agents": [
                self._build_agent_entry(planner_agent, selected=True, decision=pipeline_decision),
                self._build_agent_entry(execution_decision.route_name, selected=True, decision=execution_decision),
            ],
            "handoffs": handoffs,
            "metadata": {
                "pipeline_route": pipeline_decision.route_name,
                "execution_route": execution_decision.route_name,
                **dict(metadata or {}),
            },
        }


from functools import lru_cache


@lru_cache(maxsize=1)
def get_agent_orchestrator() -> AgentOrchestrator:
    return AgentOrchestrator()
