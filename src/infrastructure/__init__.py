# -*- coding: utf-8 -*-
"""
基础设施子包。

保持包入口轻量，避免导入任意 `src.infrastructure.*` 子模块时触发整套
基础设施预加载，尤其是 frozen 多进程子进程里会放大导入链问题。
"""

__all__: list[str] = []
