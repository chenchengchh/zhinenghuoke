"""
统一学习服务 (Unified Learning Service)

整合以下冗余模块：
1. ActiveLearner - 主动学习（底层引擎）
2. IntelligentLearningEngine - 智能学习引擎
3. LearningFacade - 学习门面
4. LearningWorkflowService - 学习工作流

提供统一的学习系统入口点，简化调用方代码。
ActiveLearner 作为底层引擎，其他模块通过此服务统一访问。
"""

from __future__ import annotations

import warnings
from concurrent.futures import Future
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# 导入原始服务（保持向后兼容）
from src.common.active_learner import (
    ActiveLearner,
    get_active_learner,
)
from src.common.intelligent_learning_engine import (
    IntelligentLearningEngine,
    get_learning_engine,
)
from src.common.learning_facade import (
    LearningFacade,
    get_learning_facade,
)
from src.common.learning_workflow_service import (
    LearningWorkflowService,
    get_learning_workflow_service,
)


class UnifiedLearningService:
    """
    统一学习服务

    整合主动学习、智能引擎、门面和工作流四大功能，
    提供简洁的统一入口点。

    核心架构：
    - ActiveLearner: 底层主动学习引擎（知识缺口检测、任务生成）
    - IntelligentLearningEngine: 智能自学引擎（事件记录、反馈闭环）
    - LearningWorkflowService: 工作流编排（审核、冲突检测等）
    - LearningFacade: 业务门面（对外暴露能力）

    推荐使用 UnifiedLearningService 替代直接使用上述任一模块。
    """

    def __init__(
        self,
        active_learner: Optional[ActiveLearner] = None,
        learning_engine: Optional[IntelligentLearningEngine] = None,
        workflow_service: Optional[LearningWorkflowService] = None,
        facade: Optional[LearningFacade] = None,
    ):
        # 初始化各组件
        self._active_learner = active_learner or get_active_learner()
        self._learning_engine = learning_engine or get_learning_engine()
        self._workflow_service = workflow_service or get_learning_workflow_service()
        self._facade = facade or get_learning_facade()

    # ==================== 主动学习功能 (ActiveLearner) ====================

    def detect_knowledge_gaps(self, enterprise_id: str = "") -> List[Dict]:
        """检测知识缺口"""
        return self._active_learner.detect_knowledge_gaps(enterprise_id)

    def generate_user_questions(self, enterprise_id: str = "") -> List[Dict]:
        """生成向用户询问的问题"""
        return self._active_learner.generate_user_questions(enterprise_id)

    def generate_user_question(self, enterprise_id: str = "") -> Optional[str]:
        """生成单个用户问题"""
        return self._active_learner.generate_user_question(enterprise_id)

    def get_learning_tasks(
        self, enterprise_id: str | int = "", limit: Optional[int] = None
    ) -> List[Dict]:
        """获取学习任务列表"""
        return self._active_learner.get_learning_tasks(enterprise_id, limit)

    def complete_task(
        self, task_id: str, answer: str, category: str = None
    ) -> bool:
        """完成学习任务"""
        return self._active_learner.complete_task(task_id, answer, category)

    def get_active_learner_stats(self) -> Dict:
        """获取主动学习统计"""
        return self._active_learner.get_stats()

    # ==================== 智能学习引擎功能 (IntelligentLearningEngine) ====================

    def on_query_received(self, query: str, session_id: str = "default") -> None:
        """当收到用户查询时调用"""
        self._learning_engine.on_query_received(query, session_id)

    def on_query_received_async(
        self, query: str, session_id: str = "default"
    ) -> Future:
        """异步记录查询学习事件"""
        return self._learning_engine.on_query_received_async(query, session_id)

    def on_knowledge_matched(
        self,
        query: str,
        matched_items: List[Tuple],
        intent: str = "unknown",
    ) -> None:
        """当知识匹配成功时调用"""
        self._learning_engine.on_knowledge_matched(query, matched_items, intent)

    def on_knowledge_matched_async(
        self,
        query: str,
        matched_items: List[Tuple],
        intent: str = "unknown",
    ) -> Future:
        """异步记录知识命中学习事件"""
        return self._learning_engine.on_knowledge_matched_async(
            query, matched_items, intent
        )

    def on_reply_generated(
        self,
        query: str,
        reply: str,
        source: str,
        matched_knowledge_id: str = None,
        session_id: str = "default",
    ) -> None:
        """当生成回复时调用"""
        self._learning_engine.on_reply_generated(
            query, reply, source, matched_knowledge_id, session_id
        )

    def on_reply_generated_async(
        self,
        query: str,
        reply: str,
        source: str,
        matched_knowledge_id: str = None,
        session_id: str = "default",
    ) -> Future:
        """异步记录回复生成学习事件"""
        return self._learning_engine.on_reply_generated_async(
            query, reply, source, matched_knowledge_id, session_id
        )

    def on_user_feedback(
        self,
        query: str,
        feedback_type: str,
        rating: int = 0,
        comment: str = "",
        knowledge_id: str = None,
        session_id: str = "default",
    ) -> None:
        """当收到用户反馈时调用"""
        self._learning_engine.on_user_feedback(
            query, feedback_type, rating, comment, knowledge_id, session_id
        )

    def on_user_feedback_async(
        self,
        query: str,
        feedback_type: str,
        rating: int = 0,
        comment: str = "",
        knowledge_id: str = None,
        session_id: str = "default",
    ) -> Future:
        """异步记录用户反馈事件"""
        return self._learning_engine.on_user_feedback_async(
            query, feedback_type, rating, comment, knowledge_id, session_id
        )

    def on_conversation_ended(
        self,
        session_id: str,
        conversation_length: int = 0,
        resolved: bool = None,
    ) -> None:
        """当对话结束时调用"""
        self._learning_engine.on_conversation_ended(
            session_id, conversation_length, resolved
        )

    def get_search_boost(self, item_id: str) -> float:
        """获取知识条目的搜索加分"""
        return self._learning_engine.get_search_boost(item_id)

    def get_effectiveness_score(self, item_id: str) -> float:
        """获取知识条目的效果评分"""
        return self._learning_engine.get_effectiveness_score(item_id)

    def get_knowledge_gaps_from_engine(self, top_n: int = 10) -> List[Dict]:
        """从引擎获取知识缺口列表"""
        return self._learning_engine.get_knowledge_gaps(top_n)

    def get_learning_stats(self) -> Dict:
        """获取学习统计"""
        return self._learning_engine.get_learning_stats()

    def get_query_suggestions(
        self, partial_query: str, top_n: int = 5
    ) -> List[str]:
        """获取查询建议"""
        return self._learning_engine.get_query_suggestions(partial_query, top_n)

    def save_learning_data(self) -> None:
        """保存学习数据"""
        self._learning_engine.save()

    def auto_fill_high_priority_gaps(self, top_n: int = 5) -> int:
        """手动触发高优先级知识缺口填充"""
        return self._learning_engine.auto_fill_high_priority_gaps(top_n)

    def convert_patterns_to_knowledge(self, min_count: int = 5) -> int:
        """将高频查询模式转化为知识条目"""
        return self._learning_engine.convert_frequent_patterns_to_knowledge(min_count)

    def apply_feedback_to_knowledge_base(self) -> None:
        """将反馈数据应用到知识库"""
        self._learning_engine.apply_feedback_to_knowledge_base()

    def shutdown_async_executor(self, wait: bool = True):
        """关闭学习异步线程池"""
        self._learning_engine.shutdown_async_executor(wait)

    # ==================== 学习工作流功能 (LearningWorkflowService) ====================

    def set_learning_enabled(self, enabled: bool) -> bool:
        """设置学习开关"""
        return self._workflow_service.set_learning_enabled(enabled)

    def get_stats_snapshot(self):
        """获取统计快照"""
        return self._workflow_service.get_stats_snapshot()

    def list_pending_validation(self, top_k: int = 20) -> List:
        """列出待验证项目"""
        return self._workflow_service.list_pending_validation(top_k)

    def approve_pending(
        self, item_id: str, approved_answer: str = "", category: str = ""
    ) -> bool:
        """批准待验证项目"""
        return self._workflow_service.approve_pending(
            item_id, approved_answer, category
        )

    def reject_pending(self, item_id: str, reason: str = "") -> bool:
        """拒绝待验证项目"""
        return self._workflow_service.reject_pending(item_id, reason)

    def clear_pending_validation(self) -> int:
        """清空待验证队列"""
        return self._workflow_service.clear_pending_validation()

    def get_history(self, limit: int = 50) -> List:
        """获取学习历史"""
        return self._workflow_service.get_history(limit)

    def apply_knowledge_decay(self) -> Dict[str, Any]:
        """应用知识衰减"""
        return self._workflow_service.apply_knowledge_decay()

    def detect_knowledge_conflicts(
        self, limit: int = 50, offset: int = 0, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """检测知识冲突"""
        return self._workflow_service.detect_knowledge_conflicts(
            limit, offset, force_refresh
        )

    def list_pending_review_knowledge(self) -> List:
        """列出待审核知识"""
        return self._workflow_service.list_pending_review_knowledge()

    def review_generated_knowledge(
        self,
        item_id: str,
        approved: bool,
        answer: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """审核生成的知识"""
        return self._workflow_service.review_generated_knowledge(
            item_id, approved, answer, reason
        )

    def generate_test_data(self) -> int:
        """生成测试数据"""
        return self._workflow_service.generate_learning_test_data()

    # ==================== 学习门面功能 (LearningFacade) ====================

    def get_enhanced_stats(self) -> Dict[str, Any]:
        """获取增强统计"""
        return self._facade.get_enhanced_stats()

    def get_quality_report(self) -> Dict[str, Any]:
        """获取质量报告"""
        return self._facade.get_quality_report()

    def submit_feedback(
        self,
        query: str,
        feedback_type: str,
        rating: int = 0,
        comment: str = "",
        knowledge_id: str = "",
        session_id: str = "default",
    ) -> bool:
        """提交学习反馈"""
        return self._facade.submit_learning_feedback(
            query, feedback_type, rating, comment, knowledge_id, session_id
        )

    def get_auto_filled_gap_records(self) -> Dict[str, Dict[str, Any]]:
        """获取自动填充缺口记录"""
        return self._facade.get_auto_filled_gap_records()

    def get_memory_tier_stats(self) -> Dict[str, Any]:
        """获取记忆层级统计"""
        return self._facade.get_memory_tier_stats()

    # ==================== 组合流程 ====================

    def full_learning_cycle(
        self,
        query: str,
        reply: str,
        source: str,
        *,
        matched_knowledge_id: str = None,
        session_id: str = "default",
        intent: str = "unknown",
        matched_items: List[Tuple] = None,
    ) -> Dict[str, Any]:
        """
        完整学习周期

        在一次交互中完成所有学习事件记录：
        1. 记录查询接收
        2. 记录知识匹配（如果有）
        3. 记录回复生成

        Returns:
            包含操作结果的字典
        """
        results = {
            "query_recorded": False,
            "knowledge_matched": False,
            "reply_recorded": False,
        }

        try:
            self.on_query_received(query, session_id)
            results["query_recorded"] = True
        except Exception as e:
            logger.warning(f"记录查询失败: {e}")

        if matched_items:
            try:
                self.on_knowledge_matched(query, matched_items, intent)
                results["knowledge_matched"] = True
            except Exception as e:
                logger.warning(f"记录知识匹配失败: {e}")

        try:
            self.on_reply_generated(
                query, reply, source, matched_knowledge_id, session_id
            )
            results["reply_recorded"] = True
        except Exception as e:
            logger.warning(f"记录回复生成失败: {e}")

        # 异步保存
        try:
            self._learning_engine.auto_save_if_needed()
        except Exception as e:
            logger.debug(f"自动保存失败: {e}")

        return results

    def check_and_suggest_improvements(
        self, enterprise_id: str = ""
    ) -> Dict[str, Any]:
        """
        检查并建议改进

        综合分析当前学习状态，提供改进建议：
        - 知识缺口检测
        - 待审核项目
        - 质量报告

        Returns:
            包含状态和建议的字典
        """
        gaps = self.detect_knowledge_gaps(enterprise_id)
        pending_validation = self.list_pending_validation(20)
        quality_report = self.get_quality_report()
        stats = self.get_stats_snapshot()

        suggestions = []

        if gaps:
            gap_count = len(gaps)
            high_severity = sum(1 for g in gaps if g.get("severity") == "high")
            suggestions.append({
                "type": "knowledge_gap",
                "priority": "high" if high_severity > 0 else "medium",
                "message": f"发现 {gap_count} 个知识缺口（{high_severity} 个高优先级）",
                "action": "auto_fill_gaps",
            })

        if len(pending_validation) > 10:
            suggestions.append({
                "type": "pending_review",
                "priority": "medium",
                "message": f"有 {len(pending_validation)} 个待验证项目待处理",
                "action": "review_pending",
            })

        engine_stats = self.get_learning_stats()
        if engine_stats.get("patterns_learned", 0) > 50:
            suggestions.append({
                "type": "pattern_conversion",
                "priority": "low",
                "message": f"已学习 {engine_stats['patterns_learned']} 个查询模式",
                "action": "convert_patterns",
            })

        return {
            "gaps": gaps,
            "pending_count": len(pending_validation),
            "quality_report": quality_report,
            "stats": stats.to_dict() if hasattr(stats, "to_dict") else stats,
            "suggestions": suggestions,
            "timestamp": datetime.now().isoformat(),
        }

    # ==================== 属性访问（向后兼容） ====================

    @property
    def active_learner(self) -> ActiveLearner:
        """访问底层主动学习器"""
        warnings.warn(
            "直接访问 active_learner 已弃用，请使用 UnifiedLearningService 的公共方法",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._active_learner

    @property
    def learning_engine(self) -> IntelligentLearningEngine:
        """访问底层智能学习引擎"""
        warnings.warn(
            "直接访问 learning_engine 已弃用，请使用 UnifiedLearningService 的公共方法",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._learning_engine

    @property
    def workflow_service(self) -> LearningWorkflowService:
        """访问底层学习工作流服务"""
        warnings.warn(
            "直接访问 workflow_service 已弃用，请使用 UnifiedLearningService 的公共方法",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._workflow_service

    @property
    def facade(self) -> LearningFacade:
        """访问底层学习门面"""
        warnings.warn(
            "直接访问 facade 已弃用，请使用 UnifiedLearningService 的公共方法",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._facade


@lru_cache(maxsize=1)
def get_unified_learning_service() -> UnifiedLearningService:
    """获取统一学习服务单例"""
    return UnifiedLearningService()


__all__ = [
    "UnifiedLearningService",
    "get_unified_learning_service",
]
