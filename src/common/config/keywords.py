"""
统一关键词配置

整合了 intent_recognizer.py 和 rag_agent.py 中的关键词定义
"""
from typing import Dict, List, Set


class IntentKeywords:
    """意图关键词配置"""
    
    GREETING_KEYWORDS: Set[str] = {
        "你好", "您好", "在吗", "有人吗", "hi", "hello",
        "早上好", "下午好", "晚上好", "哈喽", "嗨"
    }
    
    FAREWELL_KEYWORDS: Set[str] = {
        "再见", "拜拜", "下次聊", "先走了", "bye",
        "回头见", "改天聊"
    }
    
    THANKS_KEYWORDS: Set[str] = {
        "谢谢", "感谢", "多谢", "辛苦", "麻烦了",
        "太好了", "好的", "收到"
    }
    
    PRODUCT_KEYWORDS: Set[str] = {
        "产品", "功能", "特点", "介绍", "是什么",
        "有什么", "怎么样", "如何", "能做"
    }
    
    PRICE_KEYWORDS: Set[str] = {
        "价格", "多少钱", "费用", "收费", "成本",
        "贵不贵", "便宜", "优惠", "折扣"
    }
    
    COMPLAINT_KEYWORDS: Set[str] = {
        "投诉", "举报", "不满", "差评", "退款",
        "赔偿", "问题", "故障", "不好", "太差"
    }
    
    COOPERATION_KEYWORDS: Set[str] = {
        "合作", "代理", "加盟", "商务", "洽谈",
        "伙伴", "联合", "共赢"
    }
    
    PURCHASE_KEYWORDS: Set[str] = {
        "购买", "下单", "订购", "买", "要",
        "想要", "预定", "预约"
    }
    
    @classmethod
    def get_all_keywords(cls) -> Set[str]:
        """获取所有关键词"""
        return (
            cls.GREETING_KEYWORDS |
            cls.FAREWELL_KEYWORDS |
            cls.THANKS_KEYWORDS |
            cls.PRODUCT_KEYWORDS |
            cls.PRICE_KEYWORDS |
            cls.COMPLAINT_KEYWORDS |
            cls.COOPERATION_KEYWORDS |
            cls.PURCHASE_KEYWORDS
        )


class CategoryKeywords:
    """分类关键词配置"""
    
    PRODUCT_CATEGORY: Dict[str, List[str]] = {
        "功能": ["功能", "特点", "能力", "支持", "提供"],
        "价格": ["价格", "费用", "收费", "成本", "多少钱"],
        "服务": ["服务", "支持", "帮助", "客服", "售后"],
        "技术": ["技术", "接口", "API", "集成", "开发"],
        "合作": ["合作", "代理", "加盟", "商务", "渠道"]
    }
    
    @classmethod
    def get_category_for_keyword(cls, keyword: str) -> str:
        """根据关键词获取分类"""
        for category, keywords in cls.PRODUCT_CATEGORY.items():
            if keyword in keywords:
                return category
        return "general"


class BusinessKeywords:
    """业务关键词配置"""
    
    HIGH_VALUE_KEYWORDS: Set[str] = {
        "VIP", "会员", "企业版", "专业版", "高级"
    }
    
    URGENCY_KEYWORDS: Set[str] = {
        "紧急", "急", "马上", "立即", "尽快", "着急"
    }
    
    NEGATIVE_KEYWORDS: Set[str] = {
        "不好", "差", "烂", "垃圾", "失望", "不满"
    }
    
    POSITIVE_KEYWORDS: Set[str] = {
        "好", "棒", "赞", "满意", "喜欢", "推荐"
    }


class IntentPatterns:
    """意图模式配置"""
    
    GREETING_PATTERNS: List[str] = [
        r"^(你好|您好|在吗|hi|hello)",
        r"(早上好|下午好|晚上好)"
    ]
    
    FAREWELL_PATTERNS: List[str] = [
        r"(再见|拜拜|bye)",
        r"(下次聊|先走了)"
    ]
    
    THANKS_PATTERNS: List[str] = [
        r"(谢谢|感谢|多谢)",
        r"(辛苦|麻烦了)"
    ]
    
    PRODUCT_PATTERNS: List[str] = [
        r"(产品|功能).*(是什么|怎么样|介绍)",
        r"(有什么|有哪些).*(功能|特点)"
    ]
    
    PRICE_PATTERNS: List[str] = [
        r"(价格|费用|多少钱)",
        r"(收费|成本)"
    ]
    
    COMPLAINT_PATTERNS: List[str] = [
        r"(投诉|举报|不满)",
        r"(退款|赔偿)"
    ]


# 导出配置实例
intent_keywords = IntentKeywords()
category_keywords = CategoryKeywords()
business_keywords = BusinessKeywords()
intent_patterns = IntentPatterns()
