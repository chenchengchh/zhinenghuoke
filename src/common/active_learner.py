"""
主动学习模块

.. deprecated::
    请使用 :class:`src.common.unified_learning_service.UnifiedLearningService` 替代。

基于知识缺口检测和不确定性采样，主动向用户收集知识
"""

import logging
import hashlib
import json
import os
import warnings
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_active_learner_instance = None


class ActiveLearner:
    """
    主动学习器

    .. deprecated::
        请使用 :class:`src.common.unified_learning_service.UnifiedLearningService` 替代。

    通过检测知识缺口和低置信度区域，生成向用户询问的问题，
    主动收集知识以填补空白
    """

    def __init__(self, knowledge_base=None, rag_service=None):
        warnings.warn(
            "ActiveLearner 已弃用，请使用 UnifiedLearningService",
            DeprecationWarning,
            stacklevel=2,
        )
        self.knowledge_base = knowledge_base
        self.rag_service = rag_service
        self._data_dir = Path("data")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._data_dir / "active_learner_state.json"
        self._tasks: List[Dict] = []
        self._completed_tasks: List[Dict] = []
        self._gap_questions: List[Dict] = []
        self._load_state()

    @staticmethod
    def _build_task_id(
        question: str,
        category: str = "other",
        source: str = "active_learner",
        enterprise_id: str = "",
    ) -> str:
        raw = f"{source}|{enterprise_id}|{category}|{question}".encode("utf-8")
        return f"al-{hashlib.sha1(raw).hexdigest()[:12]}"

    def _normalize_task(self, task: Dict) -> Dict:
        normalized = dict(task or {})
        normalized.setdefault("question", "")
        normalized.setdefault("category", "other")
        normalized.setdefault("priority", "medium")
        normalized.setdefault("source", "active_learner")
        normalized.setdefault("status", "pending")
        normalized.setdefault("enterprise_id", "")
        normalized.setdefault("schema_id", "")
        normalized["id"] = normalized.get("id") or self._build_task_id(
            normalized.get("question", ""),
            normalized.get("category", "other"),
            normalized.get("source", "active_learner"),
            str(normalized.get("enterprise_id") or "").strip(),
        )
        return normalized

    def _load_state(self):
        """加载持久化状态"""
        try:
            if self._state_file.exists():
                with open(self._state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    self._tasks = [self._normalize_task(task) for task in state.get('tasks', [])]
                    self._completed_tasks = [self._normalize_task(task) for task in state.get('completed_tasks', [])]
                    self._gap_questions = state.get('gap_questions', [])
        except Exception as e:
            logger.warning(f"加载主动学习状态失败: {e}")

    def _save_state(self):
        """保存状态到文件"""
        try:
            state = {
                'schema_version': 2,
                'tasks': self._tasks[-100:],
                'completed_tasks': self._completed_tasks[-100:],
                'gap_questions': self._gap_questions[-200:]
            }
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存主动学习状态失败: {e}")

    def detect_knowledge_gaps(self, enterprise_id: str = "") -> List[Dict]:
        """
        检测知识缺口

        通过分析查询日志中的低置信度查询和未匹配查询，
        识别知识库中的薄弱环节
        """
        gaps = []

        try:
            if self.rag_service:
                stats = self.rag_service.get_learning_stats()
                unknown_count = stats.get('unknown_detected', 0)
                pending_count = stats.get('pending_validation', 0)
                if unknown_count > 0:
                    gaps.append({
                        'type': 'unknown_queries',
                        'description': f'有 {unknown_count} 个未知问题待学习',
                        'severity': 'high' if unknown_count > 10 else 'medium',
                        'count': unknown_count
                    })
                if pending_count > 0:
                    gaps.append({
                        'type': 'pending_validation',
                        'description': f'有 {pending_count} 条知识待审核',
                        'severity': 'medium',
                        'count': pending_count
                    })

            category_gaps = self._detect_category_gaps()
            gaps.extend(category_gaps)

            self._gap_questions = gaps
            self._save_state()

        except Exception as e:
            logger.error(f"检测知识缺口失败: {e}")

        return gaps

    def _detect_category_gaps(self) -> List[Dict]:
        """检测分类维度的知识缺口"""
        gaps = []

        try:
            if self.knowledge_base:
                category_counts = {}
                if hasattr(self.knowledge_base, 'get_knowledge_list'):
                    for item in self.knowledge_base.get_knowledge_list():
                        category = getattr(item, 'category', 'other') or 'other'
                        category_counts[category] = category_counts.get(category, 0) + 1
                categories = []
                if hasattr(self.knowledge_base, 'get_all_categories'):
                    categories = list(self.knowledge_base.get_all_categories())
                if not categories:
                    categories = sorted(category_counts.keys())

                for category in categories:
                    target = max(category_counts.get(category, 0), 3)
                    current_count = category_counts.get(category, 0)
                    if current_count < target * 0.3:
                        gaps.append({
                            'type': 'category_gap',
                            'category': category,
                            'description': f'{category} 分类知识不足 (目标: {target}, 当前: ~{current_count})',
                            'severity': 'high' if current_count < target * 0.1 else 'medium',
                            'count': target - current_count
                        })
        except Exception as e:
            logger.warning(f"检测分类缺口失败: {e}")

        return gaps

    def generate_user_questions(self, enterprise_id: str = "") -> List[Dict]:
        """
        生成向用户询问的问题

        基于知识缺口和低置信度区域，生成具体的问题
        """
        questions = []

        try:
            resolved_enterprise_id = str(enterprise_id or "").strip()
            gaps = self.detect_knowledge_gaps(resolved_enterprise_id)
            for gap in gaps:
                gap_type = gap.get('type')
                if gap_type == 'category_gap':
                    category = gap.get('category', '')
                    questions.append(self._normalize_task({
                        'question': f'请补充 {category} 分类下的高频问答知识',
                        'category': category or 'other',
                        'priority': gap.get('severity', 'medium'),
                        'source': 'active_learner',
                        'enterprise_id': resolved_enterprise_id,
                    }))
                elif gap_type == 'unknown_queries':
                    questions.append(self._normalize_task({
                        'question': '请整理近期未知问题并补充标准答案',
                        'category': 'other',
                        'priority': gap.get('severity', 'medium'),
                        'source': 'active_learner',
                        'enterprise_id': resolved_enterprise_id,
                    }))
                elif gap_type == 'pending_validation':
                    questions.append(self._normalize_task({
                        'question': '请优先审核待验证知识，避免学习链路堆积',
                        'category': 'other',
                        'priority': gap.get('severity', 'medium'),
                        'source': 'active_learner',
                        'enterprise_id': resolved_enterprise_id,
                    }))
        except Exception as e:
            logger.error(f"生成用户问题失败: {e}")

        return questions

    def generate_user_question(self, enterprise_id: str = "") -> Optional[str]:
        questions = self.generate_user_questions(enterprise_id)
        if not questions:
            return None
        return questions[0].get("question") or None

    def get_learning_tasks(self, enterprise_id: str | int = "", limit: Optional[int] = None) -> List[Dict]:
        """获取学习任务列表"""
        effective_enterprise_id = str(enterprise_id or "").strip() if isinstance(enterprise_id, str) else ""
        effective_limit = enterprise_id if isinstance(enterprise_id, int) else limit
        if not self._tasks:
            self._tasks = self.generate_user_questions(effective_enterprise_id)
            self._save_state()
        return self._tasks[:effective_limit] if effective_limit is not None else self._tasks

    def get_pending_tasks(self, limit: int = 10) -> List[Dict]:
        return self.get_learning_tasks(limit=limit)

    def complete_task(self, task_id: str, answer: str, category: str = None) -> bool:
        """完成学习任务"""
        try:
            task = next(
                (
                    t
                    for t in self._tasks
                    if t.get('id') == task_id or t.get('question') == task_id
                ),
                None,
            )
            if not task:
                return False

            task['answer'] = answer
            task['completed_at'] = datetime.now().isoformat()
            task['category'] = category or task.get('category', 'other')
            task['status'] = 'completed'

            self._completed_tasks.append(task)
            self._tasks.remove(task)

            if self.knowledge_base:
                self.knowledge_base.add_knowledge({
                    'question': task['question'],
                    'answer': answer,
                    'category': task.get('category', 'other'),
                    'keywords': [],
                    'tags': ['active_learning'],
                    'source': 'active_learning',
                    'enabled': False,
                    'enterprise_id': str(task.get('enterprise_id') or '').strip(),
                    'schema_id': str(task.get('schema_id') or '').strip(),
                })

            self._save_state()
            return True
        except Exception as e:
            logger.error(f"完成学习任务失败: {e}")
            return False

    def get_stats(self) -> Dict:
        """获取主动学习统计"""
        return {
            'pending_tasks': len(self._tasks),
            'completed_tasks': len(self._completed_tasks),
            'gap_count': len(self._gap_questions),
            'categories_with_gaps': len([g for g in self._gap_questions if g.get('type') == 'category_gap'])
        }

    def get_statistics(self) -> Dict:
        return self.get_stats()


def get_active_learner(knowledge_base=None, rag_service=None) -> ActiveLearner:
    """获取主动学习器单例"""
    global _active_learner_instance
    if _active_learner_instance is None:
        _active_learner_instance = ActiveLearner(
            knowledge_base=knowledge_base,
            rag_service=rag_service
        )
    return _active_learner_instance
