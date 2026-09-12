"""
知识库内容安全审计

提供敏感词扫描和自动脱敏能力，防止敏感信息（手机号、邮箱、QQ/微信号）
和违规内容（赌博、毒品、色情）进入知识库。

设计原则：
- 审计 + 脱敏两段式：先检测再处理，便于排查问题。
- 默认不阻断：仅记录日志并自动脱敏，避免误伤正常业务。
- 可扩展：敏感词库通过 `_SENSITIVE_PATTERNS` 列表集中维护。
"""
import logging
import re
import threading
from typing import List, Tuple

logger = logging.getLogger(__name__)


# 敏感信息正则模式：(pattern, 名称)
_SENSITIVE_PATTERNS: List[Tuple[str, str]] = [
    (r"\b1[3-9]\d{9}\b", "phone_number"),
    (r"微信[:：]?\s*[A-Za-z0-9_-]{4,}", "wechat"),
    (r"\bQQ[:：]?\s*\d{5,12}\b", "qq"),
    (r"[\w.+-]+@[\w-]+\.[\w.-]+", "email"),
    (r"\d{17}[\dXx]", "id_card"),
    (r"\b\d{16,19}\b", "bank_card"),
    (r"赌博|博彩|彩票|外围", "gambling"),
    (r"毒品|违禁|冰毒|海洛因", "drugs"),
    (r"色情|裸聊|一夜情", "porn"),
]

# 脱敏规则：(pattern, 替换文本)
_SANITIZE_RULES: List[Tuple[str, str]] = [
    (r"\b1[3-9]\d{9}\b", "[手机号]"),
    (r"微信[:：]?\s*[A-Za-z0-9_-]{4,}", "微信:[微信号]"),
    (r"\bQQ[:：]?\s*\d{5,12}\b", "QQ:[QQ号]"),
    (r"[\w.+-]+@[\w-]+\.[\w.-]+", "[邮箱]"),
    (r"\d{17}[\dXx]", "[身份证号]"),
    (r"\b\d{16,19}\b", "[银行卡号]"),
]

# 预编译正则（性能优化）
_COMPILED_SENSITIVE: List[Tuple[re.Pattern, str]] = [
    (re.compile(p), name) for p, name in _SENSITIVE_PATTERNS
]
_COMPILED_SANITIZE: List[Tuple[re.Pattern, str]] = [
    (re.compile(p)) for p, _ in _SANITIZE_RULES
]
_SANITIZE_REPLACEMENTS: List[str] = [r for _, r in _SANITIZE_RULES]


class ContentAuditor:
    """知识库内容安全审计器。"""

    def __init__(self) -> None:
        # 审计日志（线程安全）
        self._audit_log: List[dict] = []
        self._lock = threading.RLock()

    def audit(self, text: str) -> Tuple[bool, List[str]]:
        """
        审计内容是否合规。

        Args:
            text: 待审计文本

        Returns:
            (is_safe, list_of_issue_names)：
            - is_safe=True 表示未发现敏感词。
            - issue_names 列出命中的敏感词类型。
        """
        if not text:
            return True, []
        issues: List[str] = []
        text_str = str(text)
        for pattern, name in _COMPILED_SENSITIVE:
            if pattern.search(text_str):
                issues.append(name)
        return len(issues) == 0, issues

    def sanitize(self, text: str) -> str:
        """
        将敏感内容替换为占位符。

        Args:
            text: 待脱敏文本

        Returns:
            脱敏后的文本
        """
        if not text:
            return text
        sanitized = str(text)
        for pattern, replacement in zip(_COMPILED_SANITIZE, _SANITIZE_REPLACEMENTS):
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    def record_audit(self, item_id: str, issues: List[str]) -> None:
        """记录审计日志（线程安全）。"""
        if not issues:
            return
        with self._lock:
            self._audit_log.append(
                {"item_id": item_id, "issues": issues, "ts": __import__("time").time()}
            )
            # 简单上限控制，避免内存无限增长
            if len(self._audit_log) > 1000:
                self._audit_log = self._audit_log[-500:]

    def get_audit_log(self, limit: int = 100) -> List[dict]:
        """获取最近的审计日志。"""
        with self._lock:
            return list(self._audit_log[-limit:])


# 单例（线程安全）
_auditor_instance: "ContentAuditor" = None
_auditor_lock = threading.Lock()


def get_content_auditor() -> ContentAuditor:
    """获取全局内容审计器实例（懒加载线程安全单例）。"""
    global _auditor_instance
    if _auditor_instance is None:
        with _auditor_lock:
            if _auditor_instance is None:
                _auditor_instance = ContentAuditor()
    return _auditor_instance
