"""
共享安全求值工具

集中放置 _safe_math_eval 等敏感求值函数，避免各模块重复实现。
"""
import ast
import operator
import logging
from typing import Union

logger = logging.getLogger(__name__)


# 允许的二元操作符白名单
_ALLOWED_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


def safe_math_eval(expr: Union[str, int, float], max_power: int = 100) -> float:
    """
    安全的数学表达式求值，替代 eval。

    支持 +, -, *, /, //, %, **, 一元 +/-，限制指数最大值。
    不支持函数调用、变量引用、属性访问等任何高风险操作。

    Args:
        expr: 表达式字符串
        max_power: 指数最大值（防止 2**1000000 攻击）

    Returns:
        float: 求值结果

    Raises:
        TypeError: 表达式非字符串
        ValueError: 表达式包含非法节点/操作符/过大指数
    """
    if isinstance(expr, (int, float)):
        return float(expr)
    if not isinstance(expr, str):
        raise TypeError(f"表达式必须为字符串，实际: {type(expr).__name__}")
    if not expr.strip():
        raise ValueError("表达式不能为空")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e}") from e

    return _eval_node(tree.body, max_power)


def _eval_node(node: ast.AST, max_power: int) -> float:
    """递归求值 AST 节点。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"不支持的常量类型: {type(node.value).__name__}")
        return float(node.value)

    # 兼容旧 Python 的 ast.Num 节点
    if isinstance(node, ast.Num):
        return float(node.n)

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, max_power)
        right = _eval_node(node.right, max_power)
        if isinstance(node.op, ast.Pow):
            if abs(right) > max_power:
                raise ValueError(f"指数过大，最大允许 {max_power}")
            return float(operator.pow(left, right))
        op_type = type(node.op)
        if op_type not in _ALLOWED_BIN_OPS:
            raise ValueError(f"不支持的操作符: {op_type.__name__}")
        return float(_ALLOWED_BIN_OPS[op_type](left, right))

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, max_power)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ValueError(f"不支持的一元操作符: {type(node.op).__name__}")

    raise ValueError(f"不支持的节点类型: {type(node).__name__}")
