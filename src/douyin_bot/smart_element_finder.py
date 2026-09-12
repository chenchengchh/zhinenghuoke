"""
智能元素查找器

用于在网页中智能查找和定位DOM元素
"""

from typing import Optional, List, Dict, Any
from loguru import logger


class SmartElementFinder:
    """
    智能元素查找器

    提供多种元素定位策略：
    1. 按文本内容查找
    2. 按CSS选择器查找
    3. 按XPath查找
    4. 按属性查找
    """

    def __init__(self, page):
        self.page = page

    def find_element(self, selector: str, timeout: float = 10) -> Optional[Any]:
        """查找单个元素"""
        try:
            return self.page.wait_for_selector(selector, timeout=timeout * 1000)
        except Exception as e:
            logger.debug(f"查找元素失败 {selector}: {e}")
            return None

    def find_elements(self, selector: str) -> List[Any]:
        """查找多个元素"""
        try:
            return self.page.query_selector_all(selector)
        except Exception as e:
            logger.debug(f"查找元素失败 {selector}: {e}")
            return []

    def find_by_text(self, text: str, exact: bool = False) -> Optional[Any]:
        """按文本内容查找元素"""
        try:
            if exact:
                selector = f"text={text}"
            else:
                locator = self.page.get_by_text(text)
                return locator.first if locator.count() > 0 else None
            return self.find_element(selector)
        except Exception as e:
            logger.debug(f"按文本查找失败: {e}")
            return None

    def find_by_xpath(self, xpath: str) -> Optional[Any]:
        """按XPath查找元素"""
        try:
            return self.page.xpath(xpath)
        except Exception as e:
            logger.debug(f"XPath查找失败: {e}")
            return None

    def find_by_attribute(self, attr: str, value: str) -> Optional[Any]:
        """按属性查找元素"""
        try:
            selector = f"[{attr}='{value}']"
            return self.find_element(selector)
        except Exception as e:
            logger.debug(f"属性查找失败: {e}")
            return None

    def find_input_box(self, timeout: float = 5) -> Optional[Any]:
        """智能查找聊天输入框元素

        按优先级尝试多种定位策略，并排除搜索框：
        1. contenteditable div（抖音聊天页标准输入框）
        2. textarea 元素
        3. contenteditable 属性元素
        4. role=textbox 的可编辑区域

        关键修复：不直接取 .first（会命中搜索框），
        而是遍历所有匹配元素，排除搜索框后选择位置最靠下/靠右的元素。

        Args:
            timeout: 等待超时时间（秒）

        Returns:
            找到的元素或None
        """
        strategies = [
            ("contenteditable_div", 'div[contenteditable="true"]'),
            ("textarea", "textarea"),
            ("contenteditable_any", "[contenteditable='true']"),
            ("role_textbox", "[role='textbox']"),
        ]

        # 用于收集所有候选元素及其位置信息
        best_candidate = None
        best_score = -1

        for name, selector in strategies:
            try:
                locator = self.page.locator(selector)
                count = locator.count()
                if count == 0:
                    continue

                # 遍历所有匹配元素，排除搜索框
                check_count = min(count, 12)
                for index in range(check_count):
                    candidate = locator.nth(index)
                    try:
                        if not candidate.is_visible(timeout=400):
                            continue
                    except Exception:
                        continue

                    # 评估候选元素：排除搜索框，优先选择靠下/靠右的元素
                    try:
                        meta = candidate.evaluate(
                            """el => {
                                const rect = el.getBoundingClientRect();
                                const closest = (sel) => {
                                    try { return el.closest(sel); } catch(e) { return null; }
                                };
                                const searchContainer = closest('[role="search"], [class*="search"], [data-e2e*="search"], [class*="Search"], [aria-label*="搜索"]');
                                const headerContainer = closest('header, [class*="header"], [class*="Header"], [class*="toolbar"], [class*="topbar"], [class*="nav"]');
                                const msgInputContainer = closest('[data-e2e="msg-input"]');
                                const chatInputContainer = closest('[class*="chat-input"], [class*="chatInput"], [class*="composer"]');
                                const placeholder = (
                                    el.getAttribute('data-placeholder') ||
                                    el.getAttribute('placeholder') ||
                                    el.getAttribute('aria-placeholder') ||
                                    ''
                                ).toLowerCase();
                                const className = typeof el.className === 'string' ? el.className.toLowerCase() : '';
                                const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                                return {
                                    top: rect.top || 0,
                                    bottom: rect.bottom || 0,
                                    left: rect.left || 0,
                                    right: rect.right || 0,
                                    width: rect.width || 0,
                                    height: rect.height || 0,
                                    viewportWidth: window.innerWidth || 0,
                                    viewportHeight: window.innerHeight || 0,
                                    insideSearchContainer: !!searchContainer,
                                    insideHeaderContainer: !!headerContainer,
                                    insideMsgInputContainer: !!msgInputContainer,
                                    insideChatInputContainer: !!chatInputContainer,
                                    placeholder,
                                    className,
                                    ariaLabel,
                                };
                            }"""
                        )
                    except Exception:
                        continue

                    # 排除搜索框
                    inside_search = bool(meta.get("insideSearchContainer"))
                    placeholder = str(meta.get("placeholder", "") or "")
                    class_name = str(meta.get("className", "") or "")
                    aria_label = str(meta.get("ariaLabel", "") or "")

                    is_search_box = (
                        inside_search
                        or any(kw in placeholder for kw in ("搜索", "search", "查找"))
                        or any(kw in aria_label for kw in ("搜索", "search", "查找"))
                        or any(kw in class_name for kw in ("search", "搜索", "toolbar", "header", "topbar", "nav"))
                    )
                    if is_search_box:
                        continue

                    # 排除header区域元素
                    if bool(meta.get("insideHeaderContainer")):
                        continue

                    # 评分：优先选择靠下、靠右、宽度大的元素（消息输入框特征）
                    score = 0.0
                    bottom = float(meta.get("bottom", 0) or 0)
                    left = float(meta.get("left", 0) or 0)
                    width = float(meta.get("width", 0) or 0)
                    height = float(meta.get("height", 0) or 0)
                    viewport_height = float(meta.get("viewportHeight", 0) or 0)
                    viewport_width = float(meta.get("viewportWidth", 0) or 0)

                    # 位置在底部加分（消息输入框通常在页面底部）
                    if viewport_height > 0 and bottom >= viewport_height * 0.6:
                        score += 50
                    elif viewport_height > 0 and bottom >= viewport_height * 0.45:
                        score += 20
                    else:
                        score -= 30

                    # 位置在右侧加分（消息输入框在右侧，搜索框在左侧）
                    if viewport_width > 0 and left >= viewport_width * 0.4:
                        score += 30
                    else:
                        score -= 20

                    # 宽度加分（消息输入框通常较宽）
                    if width > 300:
                        score += 20
                    elif width > 200:
                        score += 10
                    elif width < 150:
                        score -= 25

                    # 高度加分
                    if height >= 32:
                        score += 10
                    elif height < 20:
                        score -= 15

                    # 精确容器加分
                    if bool(meta.get("insideMsgInputContainer")):
                        score += 100
                    if bool(meta.get("insideChatInputContainer")):
                        score += 60

                    # className包含消息/聊天相关关键词加分
                    if any(kw in class_name for kw in ("editor", "input", "chat", "message", "textbox", "composer")):
                        score += 15

                    if score > best_score:
                        best_score = score
                        best_candidate = candidate

                if best_candidate is not None and best_score >= 0:
                    logger.debug(f"输入框定位成功(策略:{name}, score={best_score:.1f}): {selector}")
                    return best_candidate
            except Exception as e:
                logger.debug(f"输入框定位失败(策略:{name}): {e}")
                continue

        # 最终fallback：等待任意contenteditable，但仍尝试排除搜索框
        try:
            fallback = self.page.wait_for_selector(
                'div[contenteditable="true"], textarea, [contenteditable="true"]',
                timeout=timeout * 1000,
            )
            if fallback:
                logger.debug("输入框定位成功(fallback)")
                return fallback
        except Exception as e:
            logger.debug(f"输入框fallback定位失败: {e}")

        logger.warning("所有输入框定位策略均失败")
        return None
