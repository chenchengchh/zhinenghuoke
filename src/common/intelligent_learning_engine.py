"""
智能自学引擎 - 让系统越用越智能

.. deprecated::
    请使用 :class:`src.common.unified_learning_service.UnifiedLearningService` 替代。

核心机制：
1. 使用频率追踪 - 每次检索后自动更新 use_count
2. 效果评分计算 - 根据多维度信号自动计算 effectiveness_score
3. 搜索权重动态调整 - 高频优质知识优先检索
4. 反馈闭环学习 - 用户反馈驱动知识质量提升
5. 知识自动优化 - 低效知识自动降权/归档
6. 查询模式学习 - 学习用户提问方式，优化检索
"""
import os
import sys
import json
import time
import threading
import re
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict
from loguru import logger

from .industry_schema_service import get_active_schema_with_compat


class LearningEvent:
    """学习事件"""

    def __init__(self, event_type: str, data: Dict):
        self.event_type = event_type
        self.data = data
        self.timestamp = time.time()

    def to_dict(self):
        return {
            "event_type": self.event_type,
            "data": self.data,
            "timestamp": self.timestamp
        }


class IntelligentLearningEngine:
    """
    智能自学引擎

    .. deprecated::
        请使用 :class:`src.common.unified_learning_service.UnifiedLearningService` 替代。

    核心思想：每次用户交互都是一次学习机会
    - 用户提问 → 记录查询模式
    - 知识匹配 → 更新使用频率
    - 用户满意 → 提升效果评分
    - 用户不满 → 降低效果评分
    - 长期不用 → 知识衰减
    """

    SYSTEM_MESSAGE_PATTERNS = [
        "对方回复或关注你之前",
        "请礼貌发言",
        "抖音自律公约",
        "商家还在等待你的回复",
        "只能发送一条文字消息",
        "对方还没有回复",
        "你赞了对方",
        "你关注了",
        "加入了群聊",
        "新成员可查看",
        "通过验证",
        "已读",
        "未读",
        "分钟前",
        "小时前",
        "昨天",
        "前天",
        "刚刚",
    ]

    UNSAFE_QUERY_PATTERN_HINTS = (
        "完整说明", "报价明细", "接收资料的联系方式", "留个微信",
        "微信或其他联系方式", "留下联系方式", "方便的话", "限时优惠", "立减",
        "您好呀，我这边专做", "热门推荐", "电话或微信通知", "请留意电话或微信通知"
    )

    UNSAFE_LEARNED_QUERY_PATTERNS = ()

    UNSAFE_LEARNED_REPLY_HINTS = ()

    UNSAFE_CROSS_INDUSTRY_REDIRECT_HINTS = ()

    DEFAULT_TOURISM_POLLUTION_QUERY_HINTS = (
        "重庆旅游客服助手",
        "旅游线路问题",
        "重庆旅游客服中心",
    )

    DEFAULT_TOURISM_POLLUTION_REPLY_HINTS = (
        "重庆旅游客服助手",
        "重庆旅游客服中心",
        "这个问题和重庆旅游线路无关",
        "重庆一日游",
        "轻轨穿楼",
        "长江索道",
    )

    EFFECTIVENESS_WEIGHTS = {
        "relevance": 0.30,
        "usage_frequency": 0.20,
        "recency": 0.15,
        "feedback_score": 0.25,
        "completeness": 0.10
    }
    
    SEARCH_BOOST = {
        "hot_item": 30.0,
        "excellent_quality": 25.0,
        "good_quality": 15.0,
        "recent_used": 10.0,
        "cold_penalty": -5.0,
        "poor_quality_penalty": -15.0
    }

    def _get_active_schema(self, enterprise_id: str = ""):
        try:
            from .industry_schema_service import IndustrySchemaService
            service = IndustrySchemaService()
            resolved_enterprise = str(enterprise_id or getattr(self, "_enterprise_id", "") or "").strip()
            return get_active_schema_with_compat(
                service,
                enterprise_id=resolved_enterprise,
            ) or {}
        except Exception:
            return {}

    def _get_active_schema_id(self, enterprise_id: str = "") -> str:
        schema = self._get_active_schema(enterprise_id=enterprise_id)
        return str(schema.get("schema_id") or "").strip()

    def _get_domain_unsafe_patterns(self, enterprise_id: str = ""):
        schema = self._get_active_schema(enterprise_id=enterprise_id)
        domain_patterns = (schema.get("metadata") or {}).get("domain_unsafe_patterns") or {}
        return {
            "query_patterns": tuple(domain_patterns.get("query_patterns", [])),
            "reply_hints": tuple(domain_patterns.get("reply_hints", [])),
            "redirect_hints": tuple(domain_patterns.get("redirect_hints", [])),
        }

    @classmethod
    def _is_unsafe_query_pattern(cls, query: str) -> bool:
        text = str(query or "").strip().lower()
        if not text:
            return True
        if any(pattern.lower() in text for pattern in cls.SYSTEM_MESSAGE_PATTERNS):
            return True
        if any(hint.lower() in text for hint in cls.UNSAFE_QUERY_PATTERN_HINTS):
            return True
        if any(hint.lower() in text for hint in cls.UNSAFE_LEARNED_QUERY_PATTERNS):
            return True
        if any(hint.lower() in text for hint in cls.DEFAULT_TOURISM_POLLUTION_QUERY_HINTS):
            return True
        if re.search(r"\d{7,}", text):
            return True
        if re.match(r"^[\w\u4e00-\u9fff]{1,20}[，,]\s*您好", text):
            return True
        return False

    @classmethod
    def _is_unsafe_learned_reply(cls, reply: str) -> bool:
        text = str(reply or "").strip()
        if not text:
            return True
        lowered = text.lower()
        if any(hint.lower() in lowered for hint in cls.UNSAFE_LEARNED_REPLY_HINTS):
            return True
        if any(hint.lower() in lowered for hint in cls.UNSAFE_CROSS_INDUSTRY_REDIRECT_HINTS):
            return True
        if any(hint.lower() in lowered for hint in cls.DEFAULT_TOURISM_POLLUTION_REPLY_HINTS):
            return True
        if any(pattern.lower() in lowered for pattern in cls.SYSTEM_MESSAGE_PATTERNS):
            return True
        return False
    
    def __init__(self, knowledge_base=None, data_dir: str = "data/learning",
                 llm_service=None, enterprise_id: str = ""):
        """
        初始化自学引擎

        .. deprecated::
            请使用 :class:`src.common.unified_learning_service.UnifiedLearningService` 替代。

        Args:
            knowledge_base: 知识库管理器
            data_dir: 学习数据存储目录
            llm_service: LLM服务（用于知识缺口自动填充）
        """
        warnings.warn(
            "IntelligentLearningEngine 已弃用，请使用 UnifiedLearningService",
            DeprecationWarning,
            stacklevel=2,
        )
        self.knowledge_base = knowledge_base
        self.data_dir = data_dir
        self.llm_service = llm_service
        self._enterprise_id = str(enterprise_id or "").strip()
        os.makedirs(data_dir, exist_ok=True)

        domain_unsafe = self._get_domain_unsafe_patterns(enterprise_id=self._enterprise_id)
        if domain_unsafe["query_patterns"]:
            self.__class__.UNSAFE_LEARNED_QUERY_PATTERNS = domain_unsafe["query_patterns"]
        if domain_unsafe["reply_hints"]:
            self.__class__.UNSAFE_LEARNED_REPLY_HINTS = domain_unsafe["reply_hints"]
        if domain_unsafe["redirect_hints"]:
            self.__class__.UNSAFE_CROSS_INDUSTRY_REDIRECT_HINTS = domain_unsafe["redirect_hints"]
        
        self._event_queue: List[LearningEvent] = []
        self._lock = threading.Lock()
        
        self._query_patterns: Dict[str, Dict] = {}
        self._load_query_patterns()
        
        self._feedback_records: Dict[str, List[Dict]] = defaultdict(list)
        self._load_feedback_records()
        
        self._session_queries: Dict[str, List[Dict]] = defaultdict(list)
        
        self._auto_filled_gaps: Dict[str, Dict] = {}
        self._load_auto_filled_gaps()
        
        self._stats = {
            "total_queries": 0,
            "knowledge_used": 0,
            "feedback_collected": 0,
            "positive_feedback": 0,
            "negative_feedback": 0,
            "quality_improvements": 0,
            "knowledge_decayed": 0,
            "patterns_learned": 0,
            "gaps_auto_filled": 0,
            "knowledge_auto_created": 0
        }
        self._load_stats()
        
        self._last_save_time = time.time()
        self._save_interval = 60
        
        self._gap_fill_cooldown: Dict[str, float] = {}
        self._gap_fill_cooldown_seconds = 300
        self._async_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="learning-engine"
        )
        self._pending_futures: List[Future] = []

    def _list_knowledge_items(self) -> List[Any]:
        if not self.knowledge_base:
            return []
        if hasattr(self.knowledge_base, "list_knowledge_items"):
            return list(self.knowledge_base.list_knowledge_items() or [])
        if hasattr(self.knowledge_base, "get_knowledge_list"):
            return list(self.knowledge_base.get_knowledge_list() or [])
        if hasattr(self.knowledge_base, "get_all_items"):
            return list(self.knowledge_base.get_all_items() or [])
        return list(getattr(self.knowledge_base, "knowledge_items", []) or [])

    def _submit_async(self, action_name: str, func, *args, auto_save: bool = True, **kwargs) -> Future:
        """将学习事件提交到单线程后台执行，避免阻塞主业务链。"""
        def runner():
            try:
                result = func(*args, **kwargs)
                if auto_save:
                    self.auto_save_if_needed()
                return result
            except Exception as exc:
                logger.warning(f"异步学习任务失败[{action_name}]: {exc}")
                raise

        future = self._async_executor.submit(runner)
        self._pending_futures = [item for item in self._pending_futures if not item.done()]
        self._pending_futures.append(future)
        return future

    def on_query_received_async(self, query: str, session_id: str = "default") -> Future:
        """异步记录查询学习事件。"""
        return self._submit_async("query_received", self.on_query_received, query, session_id)

    def on_knowledge_matched_async(
        self,
        query: str,
        matched_items: List[Tuple],
        intent: str = "unknown",
    ) -> Future:
        """异步记录知识命中学习事件。"""
        return self._submit_async(
            "knowledge_matched",
            self.on_knowledge_matched,
            query,
            matched_items,
            intent,
        )

    def on_reply_generated_async(
        self,
        query: str,
        reply: str,
        source: str,
        matched_knowledge_id: str = None,
        session_id: str = "default",
    ) -> Future:
        """异步记录回复生成学习事件。"""
        return self._submit_async(
            "reply_generated",
            self.on_reply_generated,
            query,
            reply,
            source,
            matched_knowledge_id,
            session_id,
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
        """异步记录用户反馈事件。"""
        return self._submit_async(
            "user_feedback",
            self.on_user_feedback,
            query,
            feedback_type,
            rating,
            comment,
            knowledge_id,
            session_id,
        )

    def auto_save_if_needed_async(self) -> Future:
        """异步触发按需保存。"""
        return self._submit_async("auto_save_if_needed", self.auto_save_if_needed, auto_save=False)

    def save_async(self) -> Future:
        """异步持久化学习数据。"""
        return self._submit_async("save", self.save, auto_save=False)

    def shutdown_async_executor(self, wait: bool = True):
        """关闭学习异步线程池，便于测试与进程退出时清理。"""
        self._async_executor.shutdown(wait=wait, cancel_futures=False)
    
    def _is_system_message(self, query: str) -> bool:
        """判断是否为系统消息（不应作为用户查询学习）"""
        if not query or len(query.strip()) == 0:
            return True
        query_stripped = query.strip()
        if len(query_stripped) < 2:
            return True
        for pattern in self.SYSTEM_MESSAGE_PATTERNS:
            if pattern in query_stripped:
                return True
        import re
        if re.match(r'^\d{4}/\d{2}/\d{2}$', query_stripped):
            return True
        if re.match(r'^\d{2}/\d{2}$', query_stripped):
            return True
        if re.match(r'^昨天\s*\d', query_stripped):
            return True
        if re.match(r'^\d+分钟前$', query_stripped):
            return True
        return False

    def on_query_received(self, query: str, session_id: str = "default"):
        """
        当收到用户查询时调用
        
        Args:
            query: 用户查询
            session_id: 会话ID
        """
        if self._is_system_message(query):
            logger.debug(f"跳过系统消息学习: '{query[:30]}'")
            return

        with self._lock:
            self._stats["total_queries"] += 1
            
            self._learn_query_pattern(query)
            
            self._session_queries[session_id].append({
                "query": query,
                "timestamp": time.time()
            })
            
            if len(self._session_queries[session_id]) > 20:
                self._session_queries[session_id] = self._session_queries[session_id][-20:]
            
            self._event_queue.append(LearningEvent("query_received", {
                "query": query,
                "session_id": session_id
            }))
    
    def on_knowledge_matched(self, query: str, matched_items: List[Tuple], 
                              intent: str = "unknown"):
        """
        当知识匹配成功时调用
        
        Args:
            query: 用户查询
            matched_items: 匹配到的知识条目列表 [(item, score), ...]
            intent: 识别的意图
        """
        if not self.knowledge_base:
            return

        if self._is_system_message(query):
            return

        with self._lock:
            self._stats["knowledge_used"] += len(matched_items)

            top_items = list(matched_items[:3])
            if hasattr(self.knowledge_base, "record_usages"):
                try:
                    self.knowledge_base.record_usages(
                        [item.id for item, _ in top_items if getattr(item, "id", "")]
                    )
                except Exception as e:
                    logger.debug(f"批量记录使用失败: {e}")
                    for item, _score in top_items:
                        try:
                            self.knowledge_base.record_usage(item.id)
                        except Exception as inner_exc:
                            logger.debug(f"记录使用失败: {inner_exc}")

            for item, score in top_items:
                if not hasattr(self.knowledge_base, "record_usages"):
                    try:
                        self.knowledge_base.record_usage(item.id)
                    except Exception as e:
                        logger.debug(f"记录使用失败: {e}")
                self._update_relevance_score(item.id, score)
                self._learn_query_knowledge_mapping(query, item, score, intent)
            
            self._event_queue.append(LearningEvent("knowledge_matched", {
                "query": query,
                "matched_count": len(matched_items),
                "top_score": matched_items[0][1] if matched_items else 0,
                "intent": intent
            }))
    
    def on_reply_generated(self, query: str, reply: str, source: str,
                            matched_knowledge_id: str = None,
                            session_id: str = "default"):
        """
        当生成回复时调用
        
        Args:
            query: 用户查询
            reply: 生成的回复
            source: 回复来源 (knowledge_base+llm / llm_only / knowledge_base_direct)
            matched_knowledge_id: 匹配的知识ID
            session_id: 会话ID
        """
        if self._is_system_message(query):
            return

        with self._lock:
            if source not in self._stats:
                self._stats[source] = 0
            self._stats[source] += 1
            
            is_gap = False
            if source == "llm_only":
                is_gap = True
            
            if source in ("knowledge_base+llm", "rag_llm") and not matched_knowledge_id:
                is_gap = True
            
            if reply and len(reply) < 20 and source != "knowledge_base_direct":
                is_gap = True
            
            if is_gap:
                self._record_knowledge_gap(query)
                if reply and len(reply) > 30:
                    self._try_auto_fill_gap(query, reply, source)
            
            self._event_queue.append(LearningEvent("reply_generated", {
                "query": query,
                "source": source,
                "matched_knowledge_id": matched_knowledge_id,
                "reply_length": len(reply) if reply else 0
            }))
    
    def on_user_feedback(self, query: str, feedback_type: str, 
                          rating: int = 0, comment: str = "",
                          knowledge_id: str = None,
                          session_id: str = "default"):
        """
        当收到用户反馈时调用
        
        Args:
            query: 用户查询
            feedback_type: 反馈类型 (positive/negative/neutral)
            rating: 评分 (1-5)
            comment: 评语
            knowledge_id: 关联的知识ID
            session_id: 会话ID
        """
        with self._lock:
            self._stats["feedback_collected"] += 1
            
            if feedback_type == "positive" or rating >= 4:
                self._stats["positive_feedback"] += 1
                self._apply_positive_feedback(knowledge_id, query)
            elif feedback_type == "negative" or rating <= 2:
                self._stats["negative_feedback"] += 1
                self._apply_negative_feedback(knowledge_id, query)
            
            # 记录反馈
            feedback_key = knowledge_id or query
            self._feedback_records[feedback_key].append({
                "query": query,
                "type": feedback_type,
                "feedback_type": feedback_type,
                "rating": rating,
                "comment": comment,
                "knowledge_id": knowledge_id,
                "timestamp": time.time()
            })
            
            self._event_queue.append(LearningEvent("user_feedback", {
                "query": query,
                "feedback_type": feedback_type,
                "rating": rating,
                "knowledge_id": knowledge_id
            }))
    
    def on_conversation_ended(self, session_id: str, 
                               conversation_length: int = 0,
                               resolved: bool = None):
        """
        当对话结束时调用
        
        Args:
            session_id: 会话ID
            conversation_length: 对话轮数
            resolved: 问题是否解决
        """
        with self._lock:
            # 根据对话结果更新知识效果评分
            queries = self._session_queries.get(session_id, [])
            
            if resolved is True:
                # 问题解决 - 提升相关知识的评分
                for q_data in queries:
                    self._boost_knowledge_for_resolved_query(q_data["query"])
            elif resolved is False:
                # 问题未解决 - 降低相关知识的评分
                for q_data in queries:
                    self._penalize_knowledge_for_unresolved_query(q_data["query"])
            
            # 清理会话追踪
            if session_id in self._session_queries:
                del self._session_queries[session_id]
            
            self._event_queue.append(LearningEvent("conversation_ended", {
                "session_id": session_id,
                "conversation_length": conversation_length,
                "resolved": resolved
            }))
    
    def get_search_boost(self, item_id: str) -> float:
        """
        获取知识条目的搜索加分
        
        基于使用频率、效果评分、时效性等计算
        
        Args:
            item_id: 知识条目ID
            
        Returns:
            搜索加分值
        """
        if not self.knowledge_base:
            return 0.0
        
        boost = 0.0
        
        # 查找知识条目
        item = None
        for ki in self._list_knowledge_items():
            if ki.id == item_id:
                item = ki
                break
        
        if item is None:
            return 0.0
        
        # 使用频率加分（新知识不惩罚，给予中性评分）
        use_count = getattr(item, 'use_count', 0)
        if use_count > 50:
            boost += self.SEARCH_BOOST["hot_item"]
        elif use_count > 20:
            boost += self.SEARCH_BOOST["hot_item"] * 0.7
        elif use_count > 10:
            boost += self.SEARCH_BOOST["hot_item"] * 0.4
        elif use_count > 0:
            boost += 5.0  # 使用过的知识给予小加分
        # use_count == 0 时不加分也不惩罚，给新知识公平的展示机会
        
        # 效果评分加分
        effectiveness = self._calculate_effectiveness_score(item_id)
        if effectiveness >= 0.8:
            boost += self.SEARCH_BOOST["excellent_quality"]
        elif effectiveness >= 0.6:
            boost += self.SEARCH_BOOST["good_quality"]
        elif effectiveness < 0.3:
            boost += self.SEARCH_BOOST["poor_quality_penalty"]
        
        # 时效性加分
        last_used = getattr(item, 'last_used_at', None)
        if last_used:
            try:
                if isinstance(last_used, str):
                    last_dt = datetime.fromisoformat(last_used)
                else:
                    last_dt = last_used
                days_ago = (datetime.now() - last_dt).days
                if days_ago <= 1:
                    boost += self.SEARCH_BOOST["recent_used"]
                elif days_ago <= 7:
                    boost += self.SEARCH_BOOST["recent_used"] * 0.5
            except Exception:
                pass
        
        return boost
    
    def get_effectiveness_score(self, item_id: str) -> float:
        """获取知识条目的效果评分"""
        return self._calculate_effectiveness_score(item_id)
    
    def get_knowledge_gaps(self, top_n: int = 10) -> List[Dict]:
        """
        获取知识缺口列表
        
        Returns:
            知识缺口列表，按频率排序
        """
        gaps = []
        gap_file = os.path.join(self.data_dir, "knowledge_gaps.json")
        
        if os.path.exists(gap_file):
            with open(gap_file, 'r', encoding='utf-8') as f:
                gaps = json.load(f)
        
        return sorted(gaps, key=lambda x: x.get("count", 0), reverse=True)[:top_n]
    
    def get_learning_stats(self) -> Dict:
        """获取学习统计"""
        stats = self._stats.copy()
        
        # 计算效果评分分布
        if self.knowledge_base:
            effectiveness_dist = {"excellent": 0, "good": 0, "average": 0, "poor": 0}
            for item in self._list_knowledge_items():
                score = self._calculate_effectiveness_score(item.id)
                if score >= 0.8:
                    effectiveness_dist["excellent"] += 1
                elif score >= 0.6:
                    effectiveness_dist["good"] += 1
                elif score >= 0.4:
                    effectiveness_dist["average"] += 1
                else:
                    effectiveness_dist["poor"] += 1
            stats["effectiveness_distribution"] = effectiveness_dist
            
            # 使用频率分布
            usage_dist = {"hot": 0, "warm": 0, "cold": 0}
            for item in self._list_knowledge_items():
                uc = getattr(item, 'use_count', 0)
                if uc > 10:
                    usage_dist["hot"] += 1
                elif uc >= 1:
                    usage_dist["warm"] += 1
                else:
                    usage_dist["cold"] += 1
            stats["usage_distribution"] = usage_dist
        
        return stats
    
    def get_query_suggestions(self, partial_query: str, top_n: int = 5) -> List[str]:
        """
        获取查询建议（基于学习到的查询模式）
        
        Args:
            partial_query: 部分查询
            top_n: 返回数量
            
        Returns:
            建议查询列表
        """
        suggestions = []
        partial_lower = partial_query.lower()
        
        for pattern, data in self._query_patterns.items():
            if self._is_unsafe_query_pattern(pattern):
                continue
            if partial_lower in pattern.lower() and data.get("count", 0) > 1:
                suggestions.append((pattern, data["count"]))
        
        suggestions.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in suggestions[:top_n]]
    
    def save(self):
        """保存学习数据"""
        with self._lock:
            self._save_query_patterns()
            self._save_feedback_records()
            self._save_stats()
            self._save_knowledge_gaps()
    
    def auto_save_if_needed(self):
        """如果需要则自动保存"""
        if time.time() - self._last_save_time > self._save_interval:
            self.save()
            self._last_save_time = time.time()
    
    # ==================== 内部方法 ====================
    
    def _learn_query_pattern(self, query: str):
        """学习查询模式"""
        query_key = query.strip().lower()
        if self._is_unsafe_query_pattern(query_key):
            return
        
        if query_key not in self._query_patterns:
            self._query_patterns[query_key] = {
                "count": 0,
                "first_seen": time.time(),
                "last_seen": time.time(),
                "related_intents": [],
                "best_knowledge_id": None,
                "best_score": 0
            }
        
        pattern = self._query_patterns[query_key]
        pattern["count"] += 1
        pattern["last_seen"] = time.time()
        
        if pattern["count"] >= 3:
            self._stats["patterns_learned"] += 1
    
    def _learn_query_knowledge_mapping(self, query: str, item, score: float, intent: str):
        """学习查询-知识映射"""
        query_key = query.strip().lower()
        if self._is_unsafe_query_pattern(query_key):
            return
        
        if query_key not in self._query_patterns:
            self._query_patterns[query_key] = {
                "count": 0,
                "first_seen": time.time(),
                "last_seen": time.time(),
                "related_intents": [],
                "best_knowledge_id": None,
                "best_score": 0
            }
        
        pattern = self._query_patterns[query_key]
        
        if intent and intent != "unknown" and intent not in pattern["related_intents"]:
            pattern["related_intents"].append(intent)
        
        current_best_score = pattern.get("best_score", 0)
        if score > current_best_score:
            pattern["best_knowledge_id"] = item.id
            pattern["best_score"] = score

    def get_learned_intent_for_query(self, query: str) -> Tuple[Optional[str], float]:
        """
        从学习数据中获取查询对应的意图
        
        Args:
            query: 用户查询
            
        Returns:
            (意图, 置信度) 元组，如果没有学习数据则返回 (None, 0.0)
        """
        query_key = query.strip().lower()
        pattern = self._query_patterns.get(query_key)
        
        if not pattern or pattern["count"] < 2:
            return None, 0.0
        
        if not pattern.get("related_intents"):
            return None, 0.0
        
        best_intent = pattern["related_intents"][0]
        confidence = min(pattern["count"] / 5.0, 0.8)
        
        return best_intent, confidence

    def get_learned_knowledge_for_query(self, query: str) -> Optional[str]:
        """
        从学习数据中获取查询对应的最佳知识ID
        
        Args:
            query: 用户查询
            
        Returns:
            最佳知识ID，如果没有则返回None
        """
        query_key = query.strip().lower()
        pattern = self._query_patterns.get(query_key)
        
        if not pattern or pattern["count"] < 2:
            return None
        
        return pattern.get("best_knowledge_id")

    def get_similar_learned_queries(self, query: str, top_n: int = 3) -> List[Dict]:
        """
        获取与当前查询相似的学习过的查询模式
        
        Args:
            query: 用户查询
            top_n: 返回数量
            
        Returns:
            相似查询列表，包含查询文本、意图和最佳知识ID
        """
        results = []
        query_lower = query.strip().lower()
        
        for pattern_key, pattern_data in self._query_patterns.items():
            if pattern_key == query_lower:
                continue
            if self._is_unsafe_query_pattern(pattern_key):
                continue
            
            if pattern_data.get("count", 0) < 2:
                continue
            
            if not pattern_data.get("related_intents"):
                continue
            
            similarity = 0.0
            query_chars = set(query_lower)
            pattern_chars = set(pattern_key)
            
            if query_chars & pattern_chars:
                common = len(query_chars & pattern_chars)
                total = len(query_chars | pattern_chars)
                similarity = common / total if total > 0 else 0
            
            if similarity > 0.3:
                results.append({
                    "query": pattern_key,
                    "intent": pattern_data["related_intents"][0] if pattern_data["related_intents"] else None,
                    "best_knowledge_id": pattern_data.get("best_knowledge_id"),
                    "count": pattern_data["count"],
                    "similarity": similarity
                })
        
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_n]
    
    def _update_relevance_score(self, item_id: str, search_score: float):
        """更新相关性评分"""
        # 相关性评分基于搜索得分的归一化
        # 搜索得分通常在 0-500 之间，归一化到 0-1
        normalized_score = min(search_score / 300.0, 1.0)
        
        # 记录到反馈中作为隐式正向反馈
        if item_id not in self._feedback_records:
            self._feedback_records[item_id] = []
        
        # 只保留最近10条相关性记录
        records = self._feedback_records[item_id]
        records.append({
            "type": "relevance",
            "score": normalized_score,
            "timestamp": time.time()
        })
        
        if len(records) > 10:
            self._feedback_records[item_id] = records[-10:]
    
    def _calculate_effectiveness_score(self, item_id: str) -> float:
        """
        计算知识条目的效果评分
        
        综合考虑：相关性、使用频率、时效性、反馈评分、完整性
        """
        if not self.knowledge_base:
            return 0.5
        
        item = None
        for ki in self._list_knowledge_items():
            if ki.id == item_id:
                item = ki
                break
        
        if item is None:
            return 0.5
        
        scores = {}
        
        # 1. 相关性评分（基于最近的搜索得分）
        records = self._feedback_records.get(item_id, [])
        relevance_records = [r for r in records if r.get("type") == "relevance"]
        if relevance_records:
            recent = relevance_records[-5:]
            scores["relevance"] = sum(r["score"] for r in recent) / len(recent)
        else:
            scores["relevance"] = 0.5
        
        # 2. 使用频率评分
        use_count = getattr(item, 'use_count', 0)
        if use_count > 50:
            scores["usage_frequency"] = 1.0
        elif use_count > 20:
            scores["usage_frequency"] = 0.8
        elif use_count > 10:
            scores["usage_frequency"] = 0.6
        elif use_count > 5:
            scores["usage_frequency"] = 0.4
        elif use_count > 0:
            scores["usage_frequency"] = 0.2
        else:
            scores["usage_frequency"] = 0.1
        
        # 3. 时效性评分
        last_used = getattr(item, 'last_used_at', None)
        if last_used:
            try:
                if isinstance(last_used, str):
                    last_dt = datetime.fromisoformat(last_used)
                else:
                    last_dt = last_used
                days_ago = (datetime.now() - last_dt).days
                if days_ago <= 1:
                    scores["recency"] = 1.0
                elif days_ago <= 7:
                    scores["recency"] = 0.8
                elif days_ago <= 30:
                    scores["recency"] = 0.5
                elif days_ago <= 90:
                    scores["recency"] = 0.3
                else:
                    scores["recency"] = 0.1
            except Exception:
                scores["recency"] = 0.3
        else:
            scores["recency"] = 0.1
        
        # 4. 反馈评分
        feedback_records = [
            r for r in records
            if (r.get("type") or r.get("feedback_type")) in ("positive", "negative")
        ]
        if feedback_records:
            positive = sum(
                1 for r in feedback_records
                if (r.get("type") or r.get("feedback_type")) == "positive"
            )
            total = len(feedback_records)
            scores["feedback_score"] = positive / total
        else:
            scores["feedback_score"] = 0.5
        
        # 5. 完整性评分
        answer = getattr(item, 'answer', '')
        question = getattr(item, 'question', '')
        keywords = getattr(item, 'keywords', [])
        
        completeness = 0.5
        if len(answer) > 50:
            completeness += 0.1
        if len(answer) > 200:
            completeness += 0.1
        if keywords and len(keywords) > 2:
            completeness += 0.1
        if question and len(question) > 5:
            completeness += 0.1
        scores["completeness"] = min(completeness, 1.0)
        
        # 加权计算总分
        total_score = 0.0
        for dimension, weight in self.EFFECTIVENESS_WEIGHTS.items():
            total_score += scores.get(dimension, 0.5) * weight
        
        return round(total_score, 3)
    
    def _apply_positive_feedback(self, knowledge_id: str, query: str):
        """应用正向反馈"""
        if knowledge_id:
            if knowledge_id not in self._feedback_records:
                self._feedback_records[knowledge_id] = []
            self._feedback_records[knowledge_id].append({
                "type": "positive",
                "query": query,
                "timestamp": time.time()
            })
    
    def _apply_negative_feedback(self, knowledge_id: str, query: str):
        """应用负向反馈"""
        if knowledge_id:
            if knowledge_id not in self._feedback_records:
                self._feedback_records[knowledge_id] = []
            self._feedback_records[knowledge_id].append({
                "type": "negative",
                "query": query,
                "timestamp": time.time()
            })
    
    def _boost_knowledge_for_resolved_query(self, query: str):
        """提升已解决问题的相关知识评分"""
        query_key = query.strip().lower()
        pattern = self._query_patterns.get(query_key)
        
        if pattern and pattern.get("best_knowledge_id"):
            kid = pattern["best_knowledge_id"]
            if kid not in self._feedback_records:
                self._feedback_records[kid] = []
            self._feedback_records[kid].append({
                "type": "positive",
                "query": query,
                "source": "conversation_resolved",
                "timestamp": time.time()
            })
            self._stats["quality_improvements"] += 1
    
    def _penalize_knowledge_for_unresolved_query(self, query: str):
        """降低未解决问题的相关知识评分"""
        query_key = query.strip().lower()
        pattern = self._query_patterns.get(query_key)
        
        if pattern and pattern.get("best_knowledge_id"):
            kid = pattern["best_knowledge_id"]
            if kid not in self._feedback_records:
                self._feedback_records[kid] = []
            self._feedback_records[kid].append({
                "type": "negative",
                "query": query,
                "source": "conversation_unresolved",
                "timestamp": time.time()
            })
    
    def _record_knowledge_gap(self, query: str):
        """记录知识缺口"""
        gap_file = os.path.join(self.data_dir, "knowledge_gaps.json")
        
        gaps = []
        if os.path.exists(gap_file):
            with open(gap_file, 'r', encoding='utf-8') as f:
                gaps = json.load(f)
        
        found = False
        for gap in gaps:
            if gap["query"].lower() == query.lower():
                gap["count"] += 1
                gap["last_seen"] = time.time()
                found = True
                break
        
        if not found:
            gaps.append({
                "query": query,
                "count": 1,
                "first_seen": time.time(),
                "last_seen": time.time()
            })
        
        with open(gap_file, 'w', encoding='utf-8') as f:
            json.dump(gaps, f, ensure_ascii=False, indent=2)

    def _try_auto_fill_gap(self, query: str, llm_reply: str, source: str):
        """尝试自动填充知识缺口
        
        当检测到知识缺口且LLM生成了有效回复时，将回复转化为知识条目
        """
        query_key = query.strip().lower()
        if self._is_unsafe_query_pattern(query_key):
            return
        
        last_fill_time = self._gap_fill_cooldown.get(query_key, 0)
        if time.time() - last_fill_time < self._gap_fill_cooldown_seconds:
            return
        
        if query_key in self._auto_filled_gaps:
            existing = self._auto_filled_gaps[query_key]
            if existing.get("status") == "approved":
                return
        
        if not llm_reply or len(llm_reply.strip()) < 30:
            return
        
        if any(p in llm_reply for p in ["抱歉", "无法回答", "作为AI", "我不能", "请稍后", "系统繁忙"]):
            return
        if self._is_unsafe_learned_reply(llm_reply):
            return
        
        gap_entry = {
            "query": query,
            "llm_reply": llm_reply[:500],
            "source": source,
            "status": "pending",
            "created_at": time.time(),
            "fill_count": 1
        }
        
        if query_key in self._auto_filled_gaps:
            existing = self._auto_filled_gaps[query_key]
            if existing.get("status") == "pending":
                existing["fill_count"] = existing.get("fill_count", 0) + 1
                if existing["fill_count"] >= 2:
                    gap_entry["status"] = "pending_review"
                    self._auto_create_knowledge_from_gap(query, llm_reply)
                    self._stats["gaps_auto_filled"] += 1
        else:
            self._auto_filled_gaps[query_key] = gap_entry
        
        self._gap_fill_cooldown[query_key] = time.time()
        self._save_auto_filled_gaps()

    def _auto_create_knowledge_from_gap(self, query: str, llm_reply: str):
        """从知识缺口自动创建知识条目"""
        if not self.knowledge_base:
            logger.warning("知识库未初始化，无法自动创建知识条目")
            return
        
        try:
            import uuid
            from .knowledge_base import KnowledgeItem
            
            category = self._infer_category_from_query(query)
            keywords = self._extract_keywords_from_query(query)
            enterprise_id = str(self._enterprise_id or "").strip()
            if not enterprise_id:
                logger.warning(f"跳过无租户上下文的自动补洞知识写入: '{query[:30]}'")
                return
            schema_id = self._get_active_schema_id(enterprise_id=enterprise_id)
            
            answer = self._refine_llm_answer(query, llm_reply)
            
            item = KnowledgeItem(
                id=f"learned_{uuid.uuid4().hex[:8]}",
                question=query,
                answer=answer,
                category=category,
                tags=["自动学习", "待人工审核"],
                keywords=keywords,
                aliases=[],
                reply_templates=[],
                priority=5,
                enabled=False,
                use_count=0,
                last_used_at=None,
                source="auto_learning",
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )
            
            success = self.knowledge_base.add_knowledge(item)
            
            if success:
                self._stats["knowledge_auto_created"] += 1
                logger.info(f"自动创建知识条目: '{query[:30]}' -> category={category}")
                
                query_key = query.strip().lower()
                if query_key in self._auto_filled_gaps:
                    self._auto_filled_gaps[query_key]["status"] = "pending_review"
                    self._auto_filled_gaps[query_key]["knowledge_id"] = item.id
                    self._auto_filled_gaps[query_key]["review_required"] = True
                
                self._save_auto_filled_gaps()
            else:
                logger.warning(f"自动创建知识条目失败: '{query[:30]}'")
                
        except Exception as e:
            logger.error(f"自动创建知识条目异常: {e}")

    def _infer_category_from_query(self, query: str) -> str:
        """从查询推断知识分类"""
        category_keywords = [
            ("price", ["价格", "多少钱", "费用", "收费", "报价", "包含", "含", "套餐", "包吃", "包住"]),
            ("itinerary", ["行程", "路线", "几天", "一日游", "两日游", "三日游", "五日游", "跟团", "自由行", "纯玩"]),
            ("accommodation", ["住宿", "酒店", "民宿", "住哪", "宾馆", "房间"]),
            ("food", ["美食", "小吃", "好吃", "餐厅", "火锅", "吃什么"]),
            ("transport", ["交通", "怎么去", "地铁", "机场", "高铁", "打车"]),
            ("attractions", ["景点", "好玩", "哪里好", "必去", "推荐", "打卡", "网红"]),
            ("service", ["联系", "客服", "微信", "电话", "售后", "退款", "投诉"]),
            ("tips", ["攻略", "注意", "避坑", "最佳", "什么时候"]),
        ]
        
        for category, keywords in category_keywords:
            if any(kw in query for kw in keywords):
                return category
        return "service"

    def _infer_domain_from_category(self, category: str) -> str:
        """从分类推断领域"""
        domain_map = {
            "price": "价格费用",
            "attractions": "场景介绍",
            "food": "美食推荐",
            "accommodation": "住宿推荐",
            "transport": "交通出行",
            "itinerary": "行程规划",
            "service": "服务沟通",
            "tips": "知识说明",
        }
        return domain_map.get(category, "服务沟通")

    def _extract_keywords_from_query(self, query: str) -> List[str]:
        """从查询中提取关键词"""
        try:
            import jieba
            words = jieba.cut(query)
            stop_words = {"的", "了", "吗", "呢", "啊", "是", "在", "有", "和", "与",
                         "我", "你", "他", "她", "它", "们", "这", "那", "什么", "怎么",
                         "如何", "哪", "几", "多", "少", "可以", "能", "会", "要", "想",
                         "请", "问", "一下", "知道", "告诉", "帮忙", "吗", "吧", "呢"}
            keywords = [w for w in words if len(w) > 1 and w not in stop_words]
            return keywords[:8]
        except Exception:
            return [query[:10]]

    def _refine_llm_answer(self, query: str, llm_reply: str) -> str:
        """优化LLM回复为知识库答案格式"""
        answer = llm_reply.strip()
        
        prefixes = ["亲爱的", "您好", "你好", "嘿", "朋友"]
        for prefix in prefixes:
            if answer.startswith(prefix):
                idx = answer.find("，")
                if idx > 0 and idx < 15:
                    answer = answer[idx + 1:].strip()
                elif answer.find("！") > 0 and answer.find("！") < 15:
                    answer = answer[answer.find("！") + 1:].strip()
                break
        
        suffixes = ["还有什么可以帮助您的", "如有需要请随时联系", "祝您旅途愉快"]
        for suffix in suffixes:
            idx = answer.rfind(suffix)
            if idx > 0:
                answer = answer[:idx].strip()
        
        if len(answer) > 800:
            answer = answer[:800]
        
        return answer

    def auto_fill_high_priority_gaps(self, top_n: int = 5) -> int:
        """手动触发高优先级知识缺口填充
        
        对出现次数>=3的知识缺口，使用LLM生成答案并写入知识库
        
        Args:
            top_n: 处理的最大缺口数
            
        Returns:
            成功填充的数量
        """
        if not self.llm_service or not self.knowledge_base:
            logger.warning("LLM服务或知识库未初始化，无法填充知识缺口")
            return 0
        
        gaps = self.get_knowledge_gaps(top_n=top_n * 3)
        high_priority_gaps = [g for g in gaps if g.get("count", 0) >= 3]
        
        filled = 0
        for gap in high_priority_gaps[:top_n]:
            query = gap["query"]
            query_key = query.strip().lower()
            
            if query_key in self._auto_filled_gaps:
                if self._auto_filled_gaps[query_key].get("status") == "approved":
                    continue
            
            try:
                answer = self._generate_answer_with_llm(query)
                if answer and len(answer) > 30:
                    self._auto_create_knowledge_from_gap(query, answer)
                    filled += 1
            except Exception as e:
                logger.error(f"填充知识缺口失败 '{query[:20]}': {e}")
        
        if filled > 0:
            logger.info(f"成功填充 {filled} 个高优先级知识缺口")
        
        return filled

    def _generate_answer_with_llm(self, query: str) -> Optional[str]:
        """使用LLM生成答案"""
        if not self.llm_service:
            return None
        
        try:
            prompt = f"""你是一名专业行业顾问。请根据以下用户问题，生成一个专业、详细、有用的回答。
回答要求：
1. 直接回答问题，不要加问候语
2. 内容要具体、实用
3. 如果涉及价格或范围，优先给出基于已知信息的说明，不要编造事实
4. 回答长度100-300字

用户问题：{query}

请直接给出回答："""
            
            if hasattr(self.llm_service, 'generate'):
                result = self.llm_service.generate(prompt)
            elif hasattr(self.llm_service, 'chat'):
                result = self.llm_service.chat([{"role": "user", "content": prompt}])
            elif hasattr(self.llm_service, 'ask'):
                result = self.llm_service.ask(prompt)
            else:
                return None
            
            if isinstance(result, dict):
                answer = result.get("answer") or result.get("content") or result.get("text", "")
            elif isinstance(result, str):
                answer = result
            else:
                answer = str(result) if result else ""
            
            return answer.strip() if answer else None
            
        except Exception as e:
            logger.error(f"LLM生成答案失败: {e}")
            return None

    def convert_frequent_patterns_to_knowledge(self, min_count: int = 5) -> int:
        """将高频查询模式转化为知识条目
        
        对于出现次数>=min_count且有最佳知识匹配的查询模式，
        如果知识库中还没有完全匹配的条目，则创建新条目
        
        Args:
            min_count: 最小出现次数
            
        Returns:
            转化的知识条目数
        """
        if not self.knowledge_base:
            return 0
        
        converted = 0
        for query_key, pattern in self._query_patterns.items():
            if self._is_unsafe_query_pattern(query_key):
                continue
            if pattern.get("count", 0) < min_count:
                continue
            
            if not pattern.get("best_knowledge_id"):
                continue
            
            existing = None
            for item in self._list_knowledge_items():
                if item.question.strip().lower() == query_key:
                    existing = item
                    break
            
            if existing:
                continue
            
            best_kid = pattern["best_knowledge_id"]
            best_item = None
            for item in self._list_knowledge_items():
                if item.id == best_kid:
                    best_item = item
                    break
            
            if not best_item:
                continue
            
            try:
                import uuid
                from .knowledge_base import KnowledgeItem
                enterprise_id = str(self._enterprise_id or "").strip()
                if not enterprise_id:
                    logger.warning(f"跳过无租户上下文的查询模式学习写入: '{query_key[:30]}'")
                    continue
                schema_id = self._get_active_schema_id(enterprise_id=enterprise_id)
                
                new_item = KnowledgeItem(
                    id=f"pattern_{uuid.uuid4().hex[:8]}",
                    question=query_key,
                    answer=best_item.answer,
                    category=best_item.category,
                    tags=["查询模式学习", "待人工审核"] + getattr(best_item, 'tags', []),
                    keywords=list(set(
                        self._extract_keywords_from_query(query_key) +
                        getattr(best_item, 'keywords', [])
                    ))[:10],
                    aliases=[],
                    reply_templates=[],
                    priority=max(best_item.priority - 2, 1),
                    enabled=False,
                    use_count=0,
                    last_used_at=None,
                    source="pattern_learning",
                    enterprise_id=enterprise_id,
                    schema_id=schema_id,
                )
                
                success = self.knowledge_base.add_knowledge(new_item)
                if success:
                    converted += 1
                    self._stats["knowledge_auto_created"] += 1
                    logger.info(f"查询模式转化: '{query_key[:30]}' (出现{pattern['count']}次)")
                    
            except Exception as e:
                logger.error(f"查询模式转化失败: {e}")
        
        return converted

    def apply_feedback_to_knowledge_base(self):
        """将反馈数据应用到知识库
        
        根据反馈记录调整知识条目的效果评分和优先级
        """
        if not self.knowledge_base:
            return
        
        updated = 0
        for item_id, records in self._feedback_records.items():
            if not records:
                continue
            
            positive = sum(
                1 for r in records
                if (r.get("type") or r.get("feedback_type")) in ("positive", "relevance")
            )
            negative = sum(
                1 for r in records
                if (r.get("type") or r.get("feedback_type")) == "negative"
            )
            total = positive + negative
            
            if total == 0:
                continue
            
            target_item = None
            for item in self._list_knowledge_items():
                if item.id == item_id:
                    target_item = item
                    break
            
            if not target_item:
                continue
            
            satisfaction_rate = positive / total
            
            current_priority = getattr(target_item, 'priority', 10)
            current_effectiveness = getattr(target_item, 'effectiveness_score', 0.0)
            
            if satisfaction_rate > 0.8 and total >= 3:
                new_priority = min(current_priority + 5, 30)
                new_effectiveness = min(current_effectiveness + 0.1, 1.0)
            elif satisfaction_rate < 0.3 and total >= 3:
                new_priority = max(current_priority - 5, 1)
                new_effectiveness = max(current_effectiveness - 0.1, 0.0)
            else:
                continue
            
            try:
                setattr(target_item, 'priority', new_priority)
                setattr(target_item, 'effectiveness_score', new_effectiveness)
                setattr(target_item, 'updated_at', datetime.now().isoformat())
                updated += 1
            except Exception as e:
                logger.debug(f"更新知识条目属性失败: {e}")
        
        if updated > 0:
            self._stats["quality_improvements"] += updated
            try:
                self.knowledge_base._save_knowledge()
                logger.info(f"反馈驱动知识库优化: 更新了 {updated} 条知识条目")
            except Exception as e:
                logger.warning(f"保存知识库失败: {e}")

    def get_auto_filled_gap_records(self) -> Dict[str, Dict]:
        """获取自动填充缺口记录。"""
        return dict(self._auto_filled_gaps)

    def mark_auto_filled_gap_review(self, knowledge_id: str, status: str, reason: str = "") -> bool:
        """按知识条目 ID 回写自动填充缺口的审核结果。"""
        updated = False
        reviewed_at = datetime.now().isoformat()
        for gap in self._auto_filled_gaps.values():
            if gap.get("knowledge_id") != knowledge_id:
                continue
            gap["status"] = status
            gap["reviewed_at"] = reviewed_at
            if reason:
                gap["reject_reason"] = reason
            updated = True
        if updated:
            self._save_auto_filled_gaps()
        return updated
    
    # ==================== 持久化方法 ====================
    
    def _load_query_patterns(self):
        """加载查询模式"""
        filepath = os.path.join(self.data_dir, "query_patterns.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                self._query_patterns = {
                    key: value
                    for key, value in (loaded or {}).items()
                    if not self._is_unsafe_query_pattern(key)
                }
            except Exception as e:
                logger.warning(f"加载查询模式失败: {e}")
    
    def _save_query_patterns(self):
        """保存查询模式"""
        filepath = os.path.join(self.data_dir, "query_patterns.json")
        try:
            safe_patterns = {
                key: value
                for key, value in self._query_patterns.items()
                if not self._is_unsafe_query_pattern(key)
            }
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(safe_patterns, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存查询模式失败: {e}")
    
    def _load_feedback_records(self):
        """加载反馈记录"""
        filepath = os.path.join(self.data_dir, "feedback_records.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    self._feedback_records = defaultdict(list, json.load(f))
            except Exception as e:
                logger.warning(f"加载反馈记录失败: {e}")
    
    def _save_feedback_records(self):
        """保存反馈记录"""
        filepath = os.path.join(self.data_dir, "feedback_records.json")
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(dict(self._feedback_records), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存反馈记录失败: {e}")
    
    def _load_stats(self):
        """加载学习统计"""
        filepath = os.path.join(self.data_dir, "learning_stats.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    saved_stats = json.load(f)
                    self._stats.update(saved_stats)
            except Exception as e:
                logger.warning(f"加载学习统计失败: {e}")
    
    def _save_stats(self):
        """保存学习统计"""
        filepath = os.path.join(self.data_dir, "learning_stats.json")
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存学习统计失败: {e}")
    
    def _save_knowledge_gaps(self):
        """保存知识缺口"""
        pass

    def _load_auto_filled_gaps(self):
        """加载自动填充缺口记录"""
        filepath = os.path.join(self.data_dir, "auto_filled_gaps.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    loaded = json.load(f) or {}
                self._auto_filled_gaps = {
                    key: value
                    for key, value in loaded.items()
                    if not self._is_unsafe_query_pattern((value or {}).get("query") or key)
                    and not self._is_unsafe_learned_reply((value or {}).get("llm_reply"))
                }
            except Exception as e:
                logger.warning(f"加载自动填充记录失败: {e}")

    def _save_auto_filled_gaps(self):
        """保存自动填充缺口记录"""
        filepath = os.path.join(self.data_dir, "auto_filled_gaps.json")
        try:
            safe_gaps = {
                key: value
                for key, value in self._auto_filled_gaps.items()
                if not self._is_unsafe_query_pattern((value or {}).get("query") or key)
                and not self._is_unsafe_learned_reply((value or {}).get("llm_reply"))
            }
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(safe_gaps, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存自动填充记录失败: {e}")


# ==================== 单例管理 ====================

from functools import lru_cache

@lru_cache(maxsize=8)
def get_learning_engine(knowledge_base=None, llm_service=None, enterprise_id: str = "") -> IntelligentLearningEngine:
    if knowledge_base is None:
        from src.common.knowledge_base_adapter import get_learning_knowledge_base_adapter
        knowledge_base = get_learning_knowledge_base_adapter()
    return IntelligentLearningEngine(knowledge_base, llm_service=llm_service, enterprise_id=enterprise_id)
