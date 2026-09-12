"""
因果推理引擎

用于分析对话中的因果关系和逻辑推理
"""

from typing import List, Dict, Any, Optional
from loguru import logger


class CausalReasoningEngine:
    """
    因果推理引擎

    负责从对话历史中提取因果关系，
    进行逻辑推理和上下文分析
    """

    def __init__(self):
        self.reasoning_depth = 3
        self.max_context_length = 10

    def reason(self, message: str, context: List[str]) -> Dict[str, Any]:
        """
        执行因果推理

        Args:
            message: 当前消息
            context: 对话上下文

        Returns:
            推理结果字典
        """
        result = {
            "causes": [],
            "effects": [],
            "confidence": 0.0,
            "reasoning_chain": []
        }

        try:
            cause_keywords = ["因为", "所以", "导致", "由于", "造成", "因此"]
            effect_keywords = ["所以", "因此", "于是", "就", "结果", "导致"]

            for ctx_msg in context[-self.max_context_length:]:
                for keyword in cause_keywords:
                    if keyword in ctx_msg:
                        result["causes"].append(ctx_msg)
                        break

                for keyword in effect_keywords:
                    if keyword in ctx_msg:
                        result["effects"].append(ctx_msg)
                        break

            if result["causes"] and result["effects"]:
                result["confidence"] = 0.7
            elif result["causes"] or result["effects"]:
                result["confidence"] = 0.4
            else:
                result["confidence"] = 0.2

        except Exception as e:
            logger.warning(f"因果推理异常: {e}")

        return result

    def extract_causal_chain(self, messages: List[str]) -> List[Dict[str, str]]:
        """提取因果链"""
        chain = []
        for i, msg in enumerate(messages):
            if i > 0:
                chain.append({
                    "cause": messages[i - 1],
                    "effect": msg
                })
        return chain


causal_reasoning_engine = CausalReasoningEngine()
