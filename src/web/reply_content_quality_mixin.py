"""
ReplyContentQualityMixin - 回复内容质量管控

从 BotService 中提取的回复内容清洗、去重、回声检测、风险分类等方法。
Mixin 中的方法通过 self 访问 BotService 的属性和其他方法，保持100%向后兼容。
"""
import re
import time

from typing import List
from datetime import datetime
from loguru import logger
from src.common.fallback_reply_service import get_fallback_reply_service


class ReplyContentQualityMixin:
    """回复内容质量管控 Mixin"""

    def _sanitize_reply_content(self, reply_content: str) -> str:
        """清理回复内容，移除知识库原始格式标记和无效内容"""
        if not reply_content:
            return reply_content
        reply_content = str(reply_content).replace("[FALLBACK]", "")
        # [FIX-INST:empty-line] 增强 strip：去除所有 Unicode 空白字符
        # Python str.strip() 只去 ASCII 空白（\t\n\r ），但 LLM 返回的内容可能含：
        #   - 全角空格 U+3000 "　"（中文输入法常用）
        #   - 半角不间断空格 U+00A0
        #   - 零宽字符 U+200B / U+200C / U+200D / U+FEFF
        #   - 各种 Unicode 空格（U+1680/U+2000-U+200A/U+2028/U+2029/U+202F/U+205F）
        # 这些字符会被 contenteditable 视为"内容"写入 <div> 您好</div>，
        # 抖音 IM 渲染时显示为视觉空行（前导空白 + div 的 line-height）。
        # 收敛前：只 .strip()（去 ASCII 空白），全角/零宽字符残留导致消息前空一行
        # 收敛后：用正则去除所有 Unicode 空白字符前缀/后缀
        unicode_whitespace_pattern = (
            r"^[\s\u00A0\u1680\u2000-\u200A\u200B-\u200D\u2028\u2029\u202F\u205F\u3000\uFEFF]+"
        )
        reply_content = re.sub(unicode_whitespace_pattern, "", reply_content)
        reply_content = re.sub(unicode_whitespace_pattern.replace("^", "$"), "", reply_content)
        # 兼容老逻辑：再次做 ASCII strip
        reply_content = reply_content.strip()
        lowered_reply = reply_content.lower()
        hidden_reasoning_markers = [
            "thinking process",
            "analyze the request",
            "**role:**",
            "**task:**",
            "**industry:**",
            "**constraints:**",
            "professional sales consultant",
            "general service sales",
            "travel/tourism",
            "tourism/travel",
        ]
        matched_reasoning_markers = sum(1 for marker in hidden_reasoning_markers if marker in lowered_reply)
        if matched_reasoning_markers >= 2:
            logger.warning("回复包含提示词/思维链泄漏内容，已拦截")
            return ""
        reply_content = re.sub(r"^\s*(?:回复|答复|参考回复)[：:]\s*", "", reply_content).strip()
        reply_content = re.sub(r'序号[：:]\s*\d+\s*$', '', reply_content).strip()
        reply_content = re.sub(r'序号[：:]\s*\d+', '', reply_content).strip()
        patterns_to_strip = [
            r'^[\d]+[\.、]\s*',
            r'^Q[：:]\s*',
            r'^A[：:]\s*',
            r'^[-*]\s*',
        ]
        for pat in patterns_to_strip:
            reply_content = re.sub(pat, '', reply_content, count=1).strip()
        risk_categories = self._classify_reply_content_risks(reply_content)
        if "system_capability" in risk_categories:
            logger.warning("回复包含系统功能描述，已过滤")
            return ""
        if "math_hallucination" in risk_categories:
            logger.warning("回复包含数学幻觉内容，已过滤")
            return ""
        reply_content = self._rewrite_reply_content_by_risks(reply_content, risk_categories)
        reply_content = re.sub(r'^功能支持[：:]', '', reply_content).strip()
        reply_content = re.sub(r'^功能可以[：:]', '', reply_content).strip()
        strict_math_patterns = [
            r'无法给出数学计算结果',
            r'没有包含任何具体的数学问题',
            r'从未出现任何数学问题',
            r'不存在任何数学问题',
            r'\\frac\{',
            r'\$\$',
            r'LaTeX',
        ]
        math_match_count = 0
        for pat in strict_math_patterns:
            if re.search(pat, reply_content):
                math_match_count += 1
        if math_match_count >= 3:
            logger.warning(f"回复包含数学幻觉内容({math_match_count}个匹配)，已过滤")
            return ""

        math_context_detected = math_match_count > 0 or any(
            marker in reply_content for marker in ("数学", "计算", "表达式", "公式", "LaTeX")
        )
        soft_math_patterns = [
            (r'无数学问题', '无相关话题'),
            (r'无计算结果', '暂无相关信息'),
            (r'数学表达式或运算请求', '其他问题'),
            (r'化为小数', ''),
        ]
        if math_context_detected:
            for pat, replacement in soft_math_patterns:
                if re.search(pat, reply_content):
                    reply_content = re.sub(pat, replacement, reply_content)

        # 清理知识库答案中的尾部多余内容（关键词标签、分隔线等）
        # 匹配模式：以换行开头后跟关键词标签、分隔线等内容
        trailing_patterns = [
            r'\n\s*【关键词】[^\n]*',  # 关键词标签及后续内容
            r'\n\s*─{10,}[^\n]*',      # 分隔线及后续内容
            r'\n\s*-{10,}[^\n]*',      # 短分隔线及后续内容
            r'\n\s*#{5,}[^\n]*',       # 哈希分隔线
            r'\n\s*_\.{5,}[^\n]*',    # 下划线分隔
            r'\n\s*\*[\*\s]*',        # 星号分隔
        ]
        for pattern in trailing_patterns:
            reply_content = re.sub(pattern, '', reply_content)

        reply_content = re.sub(r'\n{3,}', '\n\n', reply_content).strip()

        DOUYIN_MAX_REPLY_LENGTH = 300
        if len(reply_content) > DOUYIN_MAX_REPLY_LENGTH:
            truncated = reply_content[:DOUYIN_MAX_REPLY_LENGTH]
            sentence_end = max(
                truncated.rfind('。'),
                truncated.rfind('！'),
                truncated.rfind('？'),
                truncated.rfind('；'),
                truncated.rfind('，'),
                truncated.rfind('\n'),
            )
            if sentence_end > DOUYIN_MAX_REPLY_LENGTH * 0.5:
                truncated = truncated[:sentence_end + 1]
            logger.warning(f"回复内容超长({len(reply_content)}字符)，截断至{len(truncated)}字符")
            reply_content = truncated

        return reply_content

    def _is_duplicate_reply_content(self, conversation_id: str, reply_content: str) -> bool:
        """检查回复内容是否与最近发送的内容重复（优化版：去除模板前缀后再比较）

        修复：先去除"客户名您好！"等模板前缀后再做相似度比较，
        避免因模板前缀相同导致不同内容的回复被误判为重复。
        仅防止30秒内网络延迟导致的完全重复发送。
        """
        try:
            return self._get_outbound_recent_reply_store().is_recent_duplicate(
                conversation_id=conversation_id,
                content=reply_content,
            )
        except Exception:
            return False

    def _is_db_sent_message(self, customer_name: str, content: str) -> bool:
        """检查消息是否与数据库中我们发送的最近回复匹配（终极安全网）

        修复：短消息(<=10字符)只在10s内才视为自发送，
        避免用户发送"你好"等简单消息被误判。
        长消息匹配时间窗口也缩短到30s。
        """
        try:
            if not self.db:
                return False
            messages = self.db.get_customer_messages(customer_name)
            if not messages:
                return False
            for msg in reversed(messages[-20:]):
                if msg.get('direction') != 'outbound':
                    continue
                sent_content = msg.get('content', '')
                if not sent_content:
                    continue
                if len(sent_content) < 10 and len(content) < 10:
                    if content == sent_content:
                        msg_time = msg.get('timestamp', 0)
                        if isinstance(msg_time, str):
                            try:
                                from datetime import datetime
                                msg_time = datetime.fromisoformat(msg_time).timestamp()
                            except Exception:
                                continue
                        if msg_time and time.time() - msg_time < 10:
                            logger.info(f"DB自发送检测: 短消息匹配且在10s内，跳过: {customer_name}")
                            return True
                        continue
                if len(sent_content) < 10:
                    continue
                if content == sent_content:
                    msg_time = msg.get('timestamp', 0)
                    if isinstance(msg_time, str):
                        try:
                            from datetime import datetime
                            msg_time = datetime.fromisoformat(msg_time).timestamp()
                        except Exception:
                            continue
                    if msg_time and time.time() - msg_time < 30:
                        return True
                    continue
                if len(content) > 10 and len(sent_content) > 10:
                    min_len = min(len(content), len(sent_content), 20)
                    if content[:min_len] == sent_content[:min_len]:
                        similarity = self._calculate_similarity(content, sent_content)
                        if similarity > 0.85:
                            msg_time = msg.get('timestamp', 0)
                            if isinstance(msg_time, str):
                                try:
                                    from datetime import datetime
                                    msg_time = datetime.fromisoformat(msg_time).timestamp()
                                except Exception:
                                    continue
                            if msg_time and time.time() - msg_time < 30:
                                return True
            return False
        except Exception:
            return False

    @staticmethod
    def _has_echo_question_or_confirm_signal(content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        has_question_signal = any(q in text for q in ('？', '?', '吗', '呢', '怎么', '如何', '什么', '为什么', '哪', '多少', '能否', '可以', '能不能', '好不好'))
        has_confirm_signal = any(q in text for q in ('好的', '确认', '没问题', '是的', '对', '行', '可以', '同意'))
        return has_question_signal or has_confirm_signal

    def _is_echo_candidate_match(self, inbound_content: str, outbound_content: str) -> bool:
        content = str(inbound_content or "").strip()
        sent_content = str(outbound_content or "").strip()
        if not content or not sent_content:
            return False
        if content == sent_content:
            return True
        if len(content) > 10 and len(sent_content) > 10:
            if content in sent_content and len(content) >= len(sent_content) * 0.7:
                if not self._has_echo_question_or_confirm_signal(content):
                    return True
            min_compare = min(len(content), len(sent_content))
            if min_compare > 20:
                common_prefix_len = 0
                for i in range(min_compare):
                    if content[i] != sent_content[i]:
                        break
                    common_prefix_len = i + 1
                prefix_ratio = common_prefix_len / max(len(content), len(sent_content))
                if prefix_ratio >= 0.6:
                    similarity = self._calculate_similarity(content, sent_content)
                    if similarity > 0.85:
                        return True
            if len(set(content) & set(sent_content)) / max(len(set(content) | set(sent_content)), 1) > 0.65:
                similarity = self._calculate_similarity(content, sent_content)
                if similarity > 0.92:
                    return True
        return False

    def _iter_persisted_echo_candidates(
        self,
        *,
        conversation_id: str = "",
        limit: int = 10,
    ) -> list[dict]:
        candidates: list[dict] = []
        if not conversation_id:
            return candidates
        try:
            last_outbound = self.db.get_last_outbound_message(conversation_id)
            if last_outbound:
                candidates.append(last_outbound)
        except Exception:
            pass
        try:
            outbox_events = list(getattr(self.db, "list_outbox_events")(limit=200) or [])
            outbox_events.sort(
                key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
                reverse=True,
            )
            for item in outbox_events:
                if str(item.get("conversation_id") or "").strip() != str(conversation_id or "").strip():
                    continue
                if not str(item.get("reply_content") or "").strip():
                    continue
                candidates.append(
                    {
                        "content": item.get("reply_content", ""),
                        "created_at": item.get("updated_at") or item.get("created_at") or "",
                        "message_id": item.get("message_id", ""),
                        "logical_message_id": item.get("logical_message_id", ""),
                        "status": item.get("status", ""),
                    }
                )
                if len(candidates) >= max(int(limit or 0), 1):
                    break
        except Exception:
            pass
        return candidates

    def _is_echo_message(
        self,
        customer_name: str,
        content: str,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        """检查入站消息是否是我们最近发送的出站消息的回显（ECHO检测）

        委托给 OutboundIdempotencyService.is_echo() 进行基础匹配，
        再补充子串/前缀/高相似度，以及持久化出站记录回查等高级检测策略。
        """
        outbound_service = self._get_outbound_idempotency_service()
        if outbound_service.is_echo(
            customer_name,
            content,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        ):
            logger.info(f"ECHO检测: 入站消息匹配出站幂等缓存，跳过: {customer_name}")
            return True

        current_time = time.time()
        echo_ttl = 120 if len(content) <= 10 else 600
        recent_entries = outbound_service.get_recent_entries(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        for sent_info in recent_entries:
            sent_time = sent_info.get("time", 0)
            sent_content = sent_info.get("content", "")
            if current_time - sent_time > echo_ttl:
                continue
            if not sent_content:
                continue
            if self._is_echo_candidate_match(content, sent_content):
                logger.info(f"ECHO检测: 入站消息匹配最近出站缓存(在{echo_ttl}s内)，跳过: {customer_name}")
                return True

        if conversation_id:
            persisted_echo_ttl = max(echo_ttl, 3600)
            for sent_info in self._iter_persisted_echo_candidates(conversation_id=conversation_id, limit=8):
                sent_content = str(sent_info.get("content", "") or "").strip()
                sent_at = str(sent_info.get("created_at", "") or "").strip()
                if not sent_content or not sent_at:
                    continue
                try:
                    sent_ts = datetime.fromisoformat(sent_at).timestamp()
                except ValueError:
                    continue
                if current_time - sent_ts > persisted_echo_ttl:
                    continue
                if self._is_echo_candidate_match(content, sent_content):
                    logger.info(
                        f"ECHO检测: 入站消息匹配持久化出站记录，跳过: customer={customer_name} "
                        f"conversation_id={conversation_id or '-'} sent_at={sent_at}"
                    )
                    return True
        return False

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的字符级2-gram Jaccard相似度（支持中文）"""
        if not text1 or not text2:
            return 0.0
        t1 = text1.lower().strip()
        t2 = text2.lower().strip()
        if t1 == t2:
            return 1.0
        n = 2
        grams1 = set(t1[i:i+n] for i in range(len(t1) - n + 1))
        grams2 = set(t2[i:i+n] for i in range(len(t2) - n + 1))
        if not grams1 and not grams2:
            return 1.0
        if not grams1 or not grams2:
            return 0.0
        intersection = grams1 & grams2
        union = grams1 | grams2
        return len(intersection) / len(union) if union else 0.0
