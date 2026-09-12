"""
长期演进能力管理器

统一收口：
1. 学习统计
2. RAG 评估结果
3. Chunk 质量摘要
4. 定时任务调度状态
5. 策略版本与演进快照
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger


DEFAULT_STRATEGY_COMPONENTS = {
    "retrieval": "unified_search_v1",
    "crag": "pass_retry_no_answer_v1",
    "learning": "async_feedback_loop_v1",
    "chunking": "chunk_quality_summary_v1",
    "routing": "agent_routing_v2",
    "tracing": "span_tree_v1",
    "workflow": "durable_message_workflow_v1",
}


@dataclass
class EvolutionStrategyVersion:
    version: str
    revision: int = 1
    updated_at: str = ""
    notes: str = ""
    changed_by: str = "system"
    change_type: str = "manual"
    baseline_snapshot_id: str = ""
    rollout_status: str = "active"
    components: Dict[str, str] = field(default_factory=dict)


class EvolutionManager:
    """统一管理长期演进快照、策略版本和调度状态。"""

    SNAPSHOT_TASK_ID = "evolution_snapshot"

    def __init__(
        self,
        *,
        data_dir: str = "data/evolution",
        learning_engine: Any = None,
        rag_service: Any = None,
        task_scheduler: Any = None,
        knowledge_items_provider: Optional[Callable[[], List[Any]]] = None,
        workflow_metrics_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        self._learning_engine = learning_engine
        self._rag_service = rag_service
        self._task_scheduler = task_scheduler
        self._knowledge_items_provider = knowledge_items_provider
        self._workflow_metrics_provider = workflow_metrics_provider

        self._strategy_version_file = os.path.join(self.data_dir, "strategy_version.json")
        self._snapshot_file = os.path.join(self.data_dir, "evolution_snapshots.json")
        self._optimization_actions_file = os.path.join(self.data_dir, "optimization_actions.json")
        self._strategy_version = self._load_strategy_version()
        self._snapshots = self._load_snapshots()
        self._optimization_actions = self._load_json(self._optimization_actions_file, [])

        self._ensure_scheduler_task()

    def _load_json(self, file_path: str, default: Any) -> Any:
        if not os.path.exists(file_path):
            return default
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception as exc:
            logger.warning(f"加载演进数据失败: {file_path}, error={exc}")
            return default

    def _save_json(self, file_path: str, payload: Any):
        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _load_strategy_version(self) -> EvolutionStrategyVersion:
        stored = self._load_json(self._strategy_version_file, {})
        if not stored:
            version = EvolutionStrategyVersion(
                version="evolution-1.0.0",
                revision=1,
                updated_at=datetime.now().isoformat(),
                notes="初始化长期演进策略版本",
                changed_by="system",
                change_type="bootstrap",
                baseline_snapshot_id="",
                rollout_status="active",
                components=dict(DEFAULT_STRATEGY_COMPONENTS),
            )
            self._save_json(self._strategy_version_file, asdict(version))
            return version
        return EvolutionStrategyVersion(
            version=stored.get("version", "evolution-1.0.0"),
            revision=int(stored.get("revision", 1)),
            updated_at=stored.get("updated_at", ""),
            notes=stored.get("notes", ""),
            changed_by=stored.get("changed_by", "system"),
            change_type=stored.get("change_type", "manual"),
            baseline_snapshot_id=stored.get("baseline_snapshot_id", ""),
            rollout_status=stored.get("rollout_status", "active"),
            components=stored.get("components", dict(DEFAULT_STRATEGY_COMPONENTS)),
        )

    def _load_snapshots(self) -> List[Dict[str, Any]]:
        snapshots = self._load_json(self._snapshot_file, [])
        return snapshots if isinstance(snapshots, list) else []

    def _resolve_learning_engine(self):
        if self._learning_engine is None:
            from src.common.learning_dependency_factory import get_learning_engine

            self._learning_engine = get_learning_engine()
        return self._learning_engine

    def _resolve_rag_service(self):
        if self._rag_service is None:
            from src.common.enhanced_customer_service import get_enhanced_customer_service

            self._rag_service = get_enhanced_customer_service()
        return self._rag_service

    def _resolve_task_scheduler(self):
        if self._task_scheduler is None:
            from src.common.learning_dependency_factory import get_learning_system
            from src.common.task_scheduler import get_task_scheduler

            learning_system = get_learning_system()
            self._task_scheduler = get_task_scheduler(learning_system=learning_system)
        elif getattr(self._task_scheduler, "learning_system", None) is None:
            from src.common.learning_dependency_factory import get_learning_system

            self._task_scheduler.learning_system = get_learning_system()
        return self._task_scheduler

    def _resolve_knowledge_items(self) -> List[Any]:
        if self._knowledge_items_provider is not None:
            return list(self._knowledge_items_provider() or [])

        from src.common.knowledge_base_adapter import get_learning_knowledge_base_adapter

        adapter = get_learning_knowledge_base_adapter()
        if hasattr(adapter, "list_knowledge_items"):
            return list(adapter.list_knowledge_items() or [])
        if hasattr(adapter, "get_knowledge_list"):
            return list(adapter.get_knowledge_list() or [])
        if hasattr(adapter, "get_all_items"):
            return list(adapter.get_all_items() or [])
        return list(getattr(adapter, "knowledge_items", []) or [])

    def _build_workflow_summary(self) -> Dict[str, Any]:
        if self._workflow_metrics_provider is not None:
            try:
                payload = self._workflow_metrics_provider() or {}
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                return {"available": False, "error": str(exc)}

        try:
            from src.common.database import DatabaseManager

            db = DatabaseManager()
            workflow_runs = db.list_workflow_runs(limit=500)
            outbox_events = db.list_outbox_events(limit=500)
        except Exception as exc:
            return {"available": False, "error": str(exc)}

        workflow_status_counts: Dict[str, int] = {}
        node_status_counts: Dict[str, int] = {}
        completed_runs = 0
        resumed_runs = 0
        paused_runs = 0
        failed_runs = 0
        retry_waiting_runs = 0

        for run in workflow_runs:
            status = run.get("status", "unknown") or "unknown"
            workflow_status_counts[status] = workflow_status_counts.get(status, 0) + 1
            if status == "completed":
                completed_runs += 1
            elif status == "paused":
                paused_runs += 1
            elif status == "failed":
                failed_runs += 1
            elif status == "waiting_retry":
                retry_waiting_runs += 1

            history = run.get("history", []) or []
            if any(item.get("status") == "resumed" for item in history):
                resumed_runs += 1
            for node in (run.get("nodes", {}) or {}).values():
                node_status = node.get("status", "unknown") or "unknown"
                node_status_counts[node_status] = node_status_counts.get(node_status, 0) + 1

        outbox_status_counts: Dict[str, int] = {}
        total_retry_attempts = 0
        retry_pending = 0
        sent_outbox = 0
        failed_outbox = 0
        max_retry_count = 0
        oldest_retry_pending_at = ""
        for event in outbox_events:
            status = event.get("status", "unknown") or "unknown"
            outbox_status_counts[status] = outbox_status_counts.get(status, 0) + 1
            retry_count = int(event.get("retry_count", 0) or 0)
            total_retry_attempts += retry_count
            max_retry_count = max(max_retry_count, retry_count)
            if status == "retry_pending":
                retry_pending += 1
                next_retry_at = event.get("next_retry_at", "") or ""
                if next_retry_at and (not oldest_retry_pending_at or next_retry_at < oldest_retry_pending_at):
                    oldest_retry_pending_at = next_retry_at
            elif status == "sent":
                sent_outbox += 1
            elif status == "failed":
                failed_outbox += 1

        total_runs = len(workflow_runs)
        total_outbox = len(outbox_events)
        completion_rate = round(completed_runs / total_runs, 3) if total_runs else 0.0
        human_resume_rate = round(resumed_runs / total_runs, 3) if total_runs else 0.0
        retry_recovery_rate = round(sent_outbox / total_outbox, 3) if total_outbox else 0.0

        return {
            "available": True,
            "workflow_runs": {
                "total": total_runs,
                "status_counts": workflow_status_counts,
                "completion_rate": completion_rate,
                "paused": paused_runs,
                "failed": failed_runs,
                "waiting_retry": retry_waiting_runs,
                "human_resumed_runs": resumed_runs,
                "human_resume_rate": human_resume_rate,
                "node_status_counts": node_status_counts,
            },
            "outbox": {
                "total": total_outbox,
                "status_counts": outbox_status_counts,
                "retry_pending": retry_pending,
                "sent": sent_outbox,
                "failed": failed_outbox,
                "retry_recovery_rate": retry_recovery_rate,
                "total_retry_attempts": total_retry_attempts,
                "max_retry_count": max_retry_count,
                "oldest_retry_pending_at": oldest_retry_pending_at,
            },
        }

    def _build_chunk_quality_summary(self) -> Dict[str, Any]:
        items = self._resolve_knowledge_items()
        chunk_qualities: List[Dict[str, Any]] = []
        for item in items:
            metadata = getattr(item, "metadata", {}) or {}
            chunk_quality = metadata.get("chunkQuality")
            if isinstance(chunk_quality, dict) and chunk_quality:
                chunk_qualities.append(chunk_quality)

        if not chunk_qualities:
            return {
                "tracked_items": 0,
                "average_quality_score": 0.0,
                "average_char_count": 0.0,
                "chunk_type_distribution": {},
            }

        type_distribution: Dict[str, int] = {}
        total_quality = 0.0
        total_chars = 0
        for item in chunk_qualities:
            total_quality += float(item.get("quality_score", 0.0) or 0.0)
            total_chars += int(item.get("char_count", 0) or 0)
            chunk_type = item.get("chunk_type", "unknown")
            type_distribution[chunk_type] = type_distribution.get(chunk_type, 0) + 1

        count = len(chunk_qualities)
        return {
            "tracked_items": count,
            "average_quality_score": round(total_quality / count, 3),
            "average_char_count": round(total_chars / count, 1),
            "chunk_type_distribution": type_distribution,
        }

    def _build_scheduler_summary(self) -> Dict[str, Any]:
        scheduler = self._resolve_task_scheduler()
        all_tasks = []
        if hasattr(scheduler, "get_all_tasks"):
            all_tasks = [task.to_dict() if hasattr(task, "to_dict") else task for task in scheduler.get_all_tasks()]

        return {
            "status": scheduler.get_scheduler_status() if hasattr(scheduler, "get_scheduler_status") else {},
            "tasks": all_tasks,
        }

    def _build_rag_summary(self) -> Dict[str, Any]:
        service = self._resolve_rag_service()
        evaluator = getattr(service, "_rag_evaluator", None)
        if evaluator is None:
            return {"available": False}

        try:
            metrics_summary = evaluator.get_metrics_summary()
        except Exception as exc:
            metrics_summary = {"error": str(exc)}

        try:
            daily_stats = evaluator.get_daily_stats(datetime.now())
        except Exception as exc:
            daily_stats = {"error": str(exc)}

        return {
            "available": True,
            "metrics_summary": metrics_summary,
            "daily_stats": daily_stats,
        }

    def _build_learning_summary(self) -> Dict[str, Any]:
        engine = self._resolve_learning_engine()
        stats = engine.get_learning_stats() if hasattr(engine, "get_learning_stats") else {}
        gaps = engine.get_knowledge_gaps(10) if hasattr(engine, "get_knowledge_gaps") else []
        return {
            "stats": stats,
            "gap_count": len(gaps),
            "top_gaps": gaps[:5],
        }

    def _build_routing_summary(self) -> Dict[str, Any]:
        service = self._resolve_rag_service()
        reply_routing = service.get_routing_statistics() if hasattr(service, "get_routing_statistics") else {}

        total_reply_routes = int(reply_routing.get("total_execution_routes", 0) or 0)
        return {
            "reply_chain": reply_routing,
            "rag_pipeline": {},
            "totals": {
                "reply_execution_routes": total_reply_routes,
                "rag_execution_routes": 0,
            },
        }

    def _build_chunk_optimization_summary(
        self,
        *,
        chunk_summary: Dict[str, Any],
        rag_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        average_quality = float(chunk_summary.get("average_quality_score", 0.0) or 0.0)
        small_ratio = float(chunk_summary.get("small_chunk_ratio", 0.0) or 0.0)
        large_ratio = float(chunk_summary.get("large_chunk_ratio", 0.0) or 0.0)
        metrics_summary = rag_summary.get("metrics_summary", {}) or {}
        average_overall = float(metrics_summary.get("average_overall_score", 0.0) or 0.0)

        recommended_strategy = "recursive"
        target_chunk_size = 600
        target_overlap = 90
        if average_quality < 0.65:
            reasons.append("chunk_quality_low")
        if average_overall and average_overall < 0.65:
            reasons.append("rag_overall_low")
        if small_ratio > 0.35:
            reasons.append("too_many_small_chunks")
            recommended_strategy = "hierarchical"
            target_chunk_size = 800
            target_overlap = 120
        elif large_ratio > 0.25:
            reasons.append("too_many_large_chunks")
            recommended_strategy = "semantic"
            target_chunk_size = 480
            target_overlap = 80

        should_optimize = bool(reasons)
        return {
            "should_optimize": should_optimize,
            "recommended_strategy": recommended_strategy,
            "target_chunk_size": target_chunk_size,
            "target_overlap": target_overlap,
            "reasons": reasons,
        }

    def _build_snapshot_health(self) -> Dict[str, Any]:
        scheduler_status = self._build_scheduler_summary().get("status", {}) or {}
        latest_snapshot = self._snapshots[-1] if self._snapshots else None
        latest_snapshot_age_hours = None
        if latest_snapshot and latest_snapshot.get("timestamp"):
            try:
                latest_timestamp = datetime.fromisoformat(latest_snapshot["timestamp"])
                latest_snapshot_age_hours = round((datetime.now() - latest_timestamp).total_seconds() / 3600, 2)
            except Exception:
                latest_snapshot_age_hours = None
        health_state = "healthy"
        if latest_snapshot_age_hours is None:
            health_state = "bootstrap_required"
        elif latest_snapshot_age_hours > 12:
            health_state = "stale_snapshot"
        elif not scheduler_status.get("running", False):
            health_state = "scheduler_idle"
        return {
            "state": health_state,
            "scheduler_running": bool(scheduler_status.get("running", False)),
            "latest_snapshot_age_hours": latest_snapshot_age_hours,
        }

    def _build_data_consistency(self, *, learning_summary: Dict[str, Any], rag_summary: Dict[str, Any]) -> Dict[str, Any]:
        learning_stats = learning_summary.get("stats", {}) or {}
        metrics_summary = rag_summary.get("metrics_summary", {}) or {}
        total_queries = int(learning_stats.get("total_queries", 0) or 0)
        total_evaluations = int(metrics_summary.get("total_evaluations", 0) or 0)
        return {
            "learning_queries": total_queries,
            "rag_evaluations": total_evaluations,
            "evaluation_coverage_ratio": round(total_evaluations / total_queries, 3) if total_queries > 0 else 0.0,
        }

    def _build_structured_recommendations(
        self,
        *,
        learning_summary: Dict[str, Any],
        rag_summary: Dict[str, Any],
        chunk_summary: Dict[str, Any],
        scheduler_summary: Dict[str, Any],
        routing_summary: Dict[str, Any],
        workflow_summary: Dict[str, Any],
        chunk_optimization_summary: Dict[str, Any],
        snapshot_health: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []

        if snapshot_health.get("state") in {"bootstrap_required", "stale_snapshot"}:
            items.append({
                "title": "演进快照需要刷新",
                "severity": "high",
                "area": "governance",
                "action": "触发一次手动快照并检查调度器运行状态",
                "status": "open",
            })

        if chunk_optimization_summary.get("should_optimize"):
            items.append({
                "title": "建议触发 chunk 自动优化",
                "severity": "high",
                "area": "chunking",
                "action": f"尝试 {chunk_optimization_summary.get('recommended_strategy')} 策略并调整 chunk_size/overlap",
                "status": "open",
            })

        metrics_summary = rag_summary.get("metrics_summary", {}) or {}
        if float(metrics_summary.get("average_overall_score", 0.0) or 0.0) < 0.65:
            items.append({
                "title": "RAG 评估分偏低",
                "severity": "medium",
                "area": "rag",
                "action": "优先复核检索证据质量、rewrite 触发条件与 no-answer 门控",
                "status": "open",
            })

        workflow_runs = workflow_summary.get("workflow_runs", {}) or {}
        outbox = workflow_summary.get("outbox", {}) or {}
        if int(workflow_runs.get("paused", 0) or 0) > 0:
            items.append({
                "title": "存在待人工恢复工作流",
                "severity": "medium",
                "area": "workflow",
                "action": "优先处理 paused workflow，避免人工闸门长期堆积",
                "status": "open",
            })
        if int(outbox.get("retry_pending", 0) or 0) > 0:
            items.append({
                "title": "存在待补偿发送事件",
                "severity": "high",
                "area": "workflow",
                "action": "检查 outbox retry backlog、浏览器可用性与发送链路健康度",
                "status": "open",
            })
        if int(outbox.get("max_retry_count", 0) or 0) >= 3:
            items.append({
                "title": "补偿重试次数偏高",
                "severity": "medium",
                "area": "workflow",
                "action": "排查高频失败会话、目标页面状态与发送冷却策略",
                "status": "open",
            })

        learning_stats = learning_summary.get("stats", {}) or {}
        if int(learning_stats.get("negative_feedback", 0) or 0) > int(learning_stats.get("positive_feedback", 0) or 0):
            items.append({
                "title": "负反馈高于正反馈",
                "severity": "medium",
                "area": "learning",
                "action": "优先处理负反馈样本并回灌知识库",
                "status": "open",
            })

        if not items:
            items.append({
                "title": "长期演进状态稳定",
                "severity": "low",
                "area": "governance",
                "action": "继续观察 execution routing、chunk 质量与评估趋势",
                "status": "monitor",
            })
        return items

    def _refresh_optimization_actions(
        self,
        *,
        snapshot: Dict[str, Any],
        chunk_optimization_summary: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        actions = list(self._optimization_actions or [])
        if chunk_optimization_summary.get("should_optimize"):
            action = {
                "action_id": f"chunk-opt-{snapshot['timestamp']}",
                "type": "chunk_optimization",
                "status": "pending",
                "created_at": snapshot["timestamp"],
                "trigger": snapshot.get("trigger", "manual"),
                "strategy_version": snapshot.get("strategy_version", {}).get("version", ""),
                "recommended_strategy": chunk_optimization_summary.get("recommended_strategy", ""),
                "target_chunk_size": chunk_optimization_summary.get("target_chunk_size", 0),
                "target_overlap": chunk_optimization_summary.get("target_overlap", 0),
                "reasons": list(chunk_optimization_summary.get("reasons", []) or []),
            }
            last_action = actions[-1] if actions else {}
            if not last_action or last_action.get("reasons") != action["reasons"] or last_action.get("status") != "pending":
                actions.append(action)
                actions = actions[-50:]
                self._optimization_actions = actions
                self._save_json(self._optimization_actions_file, actions)
        return list(reversed(actions[-10:]))

    def _build_recommendations(
        self,
        *,
        learning_summary: Dict[str, Any],
        rag_summary: Dict[str, Any],
        chunk_summary: Dict[str, Any],
        scheduler_summary: Dict[str, Any],
        routing_summary: Dict[str, Any],
        workflow_summary: Dict[str, Any],
    ) -> List[str]:
        recommendations: List[str] = []

        average_quality = float(chunk_summary.get("average_quality_score", 0.0) or 0.0)
        if chunk_summary.get("tracked_items", 0) > 0 and average_quality < 0.7:
            recommendations.append("chunk 质量均分偏低，建议继续优化 chunk 大小、重叠率和结构化切分策略。")

        gap_count = int(learning_summary.get("gap_count", 0) or 0)
        if gap_count >= 3:
            recommendations.append("知识缺口数量较多，建议优先执行自动补缺与人工审核闭环。")

        learning_stats = learning_summary.get("stats", {}) or {}
        negative_feedback = int(learning_stats.get("negative_feedback", 0) or 0)
        positive_feedback = int(learning_stats.get("positive_feedback", 0) or 0)
        if negative_feedback > positive_feedback and negative_feedback > 0:
            recommendations.append("负反馈高于正反馈，建议优先应用反馈到知识库并复核低效知识。")

        metrics_summary = rag_summary.get("metrics_summary", {}) or {}
        average_overall = float(metrics_summary.get("average_overall_score", 0.0) or 0.0)
        if metrics_summary.get("total_evaluations", 0) and average_overall < 0.65:
            recommendations.append("RAG 综合得分偏低，建议优先复核检索证据质量和 no-answer 门控阈值。")
        elif not metrics_summary.get("total_evaluations"):
            recommendations.append("当前缺少稳定评估样本，建议持续积累 RAG 评估数据作为长期演进基线。")

        scheduler_status = scheduler_summary.get("status", {}) or {}
        if not scheduler_status.get("running", False):
            recommendations.append("长期演进调度器尚未运行，建议启动定时任务以形成持续快照和回灌闭环。")

        reply_handoffs = (routing_summary.get("reply_chain", {}) or {}).get("handoff_reasons", {}) or {}
        rag_handoffs = (routing_summary.get("rag_pipeline", {}) or {}).get("handoff_reasons", {}) or {}
        total_handoffs = sum(int(v or 0) for v in reply_handoffs.values()) + sum(int(v or 0) for v in rag_handoffs.values())
        if total_handoffs > 0:
            recommendations.append("已观察到 agent handoff/fallback，建议结合 execution routing 统计持续优化升级阈值与兜底策略。")

        workflow_runs = workflow_summary.get("workflow_runs", {}) or {}
        outbox = workflow_summary.get("outbox", {}) or {}
        if int(workflow_runs.get("paused", 0) or 0) > 0:
            recommendations.append("存在 paused workflow，建议建立人工恢复 SLA 并优先处理 need_human 积压。")
        if int(outbox.get("retry_pending", 0) or 0) > 0:
            recommendations.append("存在待补偿 outbox 事件，建议优先检查浏览器在线状态、页面可操作性与发送链路稳定性。")
        if float(outbox.get("retry_recovery_rate", 0.0) or 0.0) < 0.8 and int(outbox.get("total", 0) or 0) >= 3:
            recommendations.append("出站补偿恢复率偏低，建议把补偿成功率纳入长期演进灰度阈值并持续观测。")

        if not recommendations:
            recommendations.append("当前长期演进指标总体稳定，可继续推进完整 Agent Routing 与策略灰度。")

        return recommendations

    def _ensure_scheduler_task(self):
        scheduler = self._resolve_task_scheduler()
        if not hasattr(scheduler, "get_task") or not hasattr(scheduler, "register_task"):
            return

        if scheduler.get_task(self.SNAPSHOT_TASK_ID):
            return

        scheduler.register_task(
            task_id=self.SNAPSHOT_TASK_ID,
            name="长期演进快照",
            description="定期汇总学习、评估、chunk 质量与调度状态，形成长期演进快照",
            callback=lambda: self.create_snapshot(trigger="scheduler", persist=True),
            interval_seconds=6 * 3600,
        )

    def get_strategy_version(self) -> Dict[str, Any]:
        return asdict(self._strategy_version)

    def bump_strategy_version(
        self,
        *,
        notes: str = "",
        component_overrides: Optional[Dict[str, str]] = None,
        changed_by: str = "system",
        change_type: str = "manual",
        baseline_snapshot_id: str = "",
        rollout_status: str = "active",
    ) -> Dict[str, Any]:
        self._strategy_version.revision += 1
        self._strategy_version.version = f"evolution-1.0.{self._strategy_version.revision - 1}"
        self._strategy_version.updated_at = datetime.now().isoformat()
        if notes:
            self._strategy_version.notes = notes
        self._strategy_version.changed_by = changed_by
        self._strategy_version.change_type = change_type
        self._strategy_version.baseline_snapshot_id = baseline_snapshot_id
        self._strategy_version.rollout_status = rollout_status
        if component_overrides:
            self._strategy_version.components.update(component_overrides)
        self._save_json(self._strategy_version_file, asdict(self._strategy_version))
        return asdict(self._strategy_version)

    def create_snapshot(self, *, trigger: str = "manual", persist: bool = True) -> Dict[str, Any]:
        learning_summary = self._build_learning_summary()
        rag_summary = self._build_rag_summary()
        chunk_summary = self._build_chunk_quality_summary()
        scheduler_summary = self._build_scheduler_summary()
        routing_summary = self._build_routing_summary()
        workflow_summary = self._build_workflow_summary()
        snapshot_health = self._build_snapshot_health()
        data_consistency = self._build_data_consistency(
            learning_summary=learning_summary,
            rag_summary=rag_summary,
        )
        chunk_optimization_summary = self._build_chunk_optimization_summary(
            chunk_summary=chunk_summary,
            rag_summary=rag_summary,
        )
        recommendations = self._build_recommendations(
            learning_summary=learning_summary,
            rag_summary=rag_summary,
            chunk_summary=chunk_summary,
            scheduler_summary=scheduler_summary,
            routing_summary=routing_summary,
            workflow_summary=workflow_summary,
        )
        structured_recommendations = self._build_structured_recommendations(
            learning_summary=learning_summary,
            rag_summary=rag_summary,
            chunk_summary=chunk_summary,
            scheduler_summary=scheduler_summary,
            routing_summary=routing_summary,
            workflow_summary=workflow_summary,
            chunk_optimization_summary=chunk_optimization_summary,
            snapshot_health=snapshot_health,
        )

        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "trigger": trigger,
            "strategy_version": self.get_strategy_version(),
            "learning": learning_summary,
            "rag": rag_summary,
            "routing": routing_summary,
            "workflow": workflow_summary,
            "chunking": chunk_summary,
            "scheduler": scheduler_summary,
            "recommendations": recommendations,
            "governance": {
                "snapshot_health": snapshot_health,
                "data_consistency": data_consistency,
                "structured_recommendations": structured_recommendations,
                "chunk_optimization": chunk_optimization_summary,
            },
        }
        snapshot["governance"]["pending_optimizations"] = self._refresh_optimization_actions(
            snapshot=snapshot,
            chunk_optimization_summary=chunk_optimization_summary,
        )

        if persist:
            self._snapshots.append(snapshot)
            self._snapshots = self._snapshots[-50:]
            self._save_json(self._snapshot_file, self._snapshots)
        return snapshot

    def get_snapshot_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        return list(reversed(self._snapshots[-limit:]))

    def get_summary(self) -> Dict[str, Any]:
        latest_snapshot = self._snapshots[-1] if self._snapshots else None
        if latest_snapshot is None:
            latest_snapshot = self.create_snapshot(trigger="summary_bootstrap", persist=True)

        return {
            "strategy_version": self.get_strategy_version(),
            "latest_snapshot": latest_snapshot,
            "history_count": len(self._snapshots),
            "has_scheduler_task": bool(self._resolve_task_scheduler().get_task(self.SNAPSHOT_TASK_ID)),
            "pending_optimizations": list(reversed((self._optimization_actions or [])[-10:])),
            "snapshot_health": latest_snapshot.get("governance", {}).get("snapshot_health", {}),
        }


_evolution_manager_instance: Optional[EvolutionManager] = None


def get_evolution_manager() -> EvolutionManager:
    global _evolution_manager_instance
    if _evolution_manager_instance is None:
        _evolution_manager_instance = EvolutionManager()
    return _evolution_manager_instance
