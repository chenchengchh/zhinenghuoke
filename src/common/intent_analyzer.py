"""
意向分析模块

对客户进行多维度分析，评估采购意向等级
实现智能自动回复功能
"""
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from loguru import logger
import re

from .context_understanding import context_understanding_module
from .enhanced_customer_service import resolve_context_session_id
from .intent_scoring_policy import INTENT_LEVEL_SCORE_FLOORS
from .types.intent import UrgencyLevel as _UrgencyLevel
from .types.intent import EmotionType as _EmotionType


class IntentLevel(Enum):
    """意向等级"""
    A = "A"  # 高意向 - 立即跟进
    B = "B"  # 中高意向 - 优先跟进
    C = "C"  # 中等意向 - 持续培育
    D = "D"  # 低意向 - 定期触达
    E = "E"  # 无意向 - 继续观察


class PurchaseIntentType(Enum):
    """购买意向类型（与统一IntentType区分）"""
    PURCHASE = "purchase"           # 购买意图
    INQUIRY = "inquiry"             # 咨询意图
    COMPARISON = "comparison"       # 对比意图
    COOPERATION = "cooperation"     # 合作意图
    COMPLAINT = "complaint"         # 投诉意图
    SUPPORT = "support"             # 售后意图
    REJECTION = "rejection"         # 拒绝意图
    INFORMATION = "information"     # 信息获取
    DEMO_REQUEST = "demo_request"   # 试用请求
    CONTACT = "contact"             # 联系意图


class IntentStrength(Enum):
    """意图强度"""
    VERY_STRONG = "very_strong"     # 非常强烈
    STRONG = "strong"               # 强烈
    MODERATE = "moderate"           # 中等
    WEAK = "weak"                   # 微弱
    NONE = "none"                   # 无意图


class RejectionType(Enum):
    """拒绝类型"""
    PRICE = "price"                 # 价格拒绝
    TIMING = "timing"               # 时间不合适
    COMPETITOR = "competitor"       # 已选择竞品
    NOT_INTERESTED = "not_interested"  # 不感兴趣
    BUDGET = "budget"               # 预算不足
    AUTHORITY = "authority"         # 无决策权
    NEED = "need"                   # 无需求


@dataclass
class IntentScore:
    """意向评分"""
    total_score: float = 0
    behavior_score: float = 0
    semantic_score: float = 0
    interaction_score: float = 0
    emotion_score: float = 0
    urgency_score: float = 0
    stage_score: float = 0
    confidence: float = 0


@dataclass
class IntentDetail:
    """意图详情"""
    intent_type: PurchaseIntentType
    strength: IntentStrength
    score: float
    keywords_matched: List[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class IntentAnalysisResult:
    """意向分析结果"""
    customer_id: str
    platform: str
    level: IntentLevel
    score: IntentScore
    factors: Dict[str, Any] = field(default_factory=dict)
    primary_intent: Optional[IntentDetail] = None
    secondary_intents: List[IntentDetail] = field(default_factory=list)
    rejection_info: Optional[Dict[str, Any]] = None
    predicted_next_action: Optional[str] = None
    recommended_response_type: str = "general"
    updated_at: datetime = field(default_factory=datetime.now)


EmotionType = _EmotionType


UrgencyLevel = _UrgencyLevel


class PurchaseStage(Enum):
    """购买阶段"""
    AWARENESS = "awareness"
    INTEREST = "interest"
    CONSIDERATION = "consideration"
    INTENT = "intent"
    DECISION = "decision"


class IntentAnalyzer:
    """
    意向分析器
    
    基于规则引擎 + 评分模型进行客户意向分析
    """
    
    LEVEL_THRESHOLDS = {
        IntentLevel.A: INTENT_LEVEL_SCORE_FLOORS["A"],
        IntentLevel.B: INTENT_LEVEL_SCORE_FLOORS["B"],
        IntentLevel.C: INTENT_LEVEL_SCORE_FLOORS["C"],
        IntentLevel.D: INTENT_LEVEL_SCORE_FLOORS["D"],
        IntentLevel.E: INTENT_LEVEL_SCORE_FLOORS["E"],
    }
    
    WEIGHTS = {
        "behavior": 0.15,
        "semantic": 0.20,
        "interaction": 0.10
    }
    
    INTENT_KEYWORDS = {
        "cooperation": [
            "合作", "加盟", "代理", "批发", "采购", "大量", "长期",
            "经销", "分销", "渠道", "代理权", "加盟商", "合作伙伴",
            "OEM", "贴牌", "定制", "代工", "招商", "招募", "签约",
            "合同", "协议", "授权", "独家", "区域代理", "总代理",
            "一级代理", "市级代理", "省级代理", "合伙人", "联营",
            "战略合作", "商务合作", "项目合作", "渠道合作", "代理申请",
            "加盟申请", "代理条件", "加盟条件", "代理费用", "加盟费",
            "保证金", "返点", "提成", "佣金", "分成", "代理政策",
            "加盟政策", "扶持", "培训支持", "市场支持", "技术支持"
        ],
        "purchase": [
            "购买", "买", "订单", "付款", "价格", "下单", "订购",
            "要买", "想买", "准备买", "打算买", "需要买", "想要",
            "成交", "交易", "支付", "结账", "买单", "购入", "入手",
            "采购", "进货", "补货", "下单子", "开单", "定下来",
            "确定要", "马上买", "立即购买", "怎么买", "哪里买",
            "订购", "预订", "预定", "预购", "抢购", "团购", "拼单",
            "批量采购", "集中采购", "年度采购", "季度采购", "月度采购",
            "续订", "追加订单", "补单", "加购", "复购", "再次购买",
            "老客户", "回头客", "老用户", "会员购买", "VIP购买",
        ],
        "inquiry": [
            "咨询", "了解", "看看", "怎么样", "请问", "咨询下",
            "问一下", "打听", "了解下", "想知道", "请教", "询问",
            "求教", "帮忙", "帮我看", "给我介绍", "介绍一下",
            "详细说说", "具体说说", "讲讲", "说说看", "能否告知",
            "能说下吗", "可以问吗", "想了解", "想咨询", "求解答",
            "求指教", "求帮助", "在线咨询", "业务咨询", "产品咨询",
            "技术咨询", "服务咨询", "售前咨询", "售后咨询",
            "想问下", "有个问题", "有个疑问", "不太清楚", "不太明白",
            "能解释下吗", "能说明下吗", "能详细说明吗", "麻烦解答",
        ],
        "price": [
            "价格", "多少钱", "收费", "便宜", "贵", "优惠", "折扣",
            "报价", "价位", "费用", "成本", "预算", "定价", "标价",
            "售价", "单价", "总价", "付款方式", "分期", "首付",
            "便宜点", "打折", "促销", "特价", "活动价", "团购价",
            "批发价", "零售价", "会员价", "优惠价", "最低价",
            "能便宜吗", "有优惠吗", "有折扣吗", "怎么收费",
            "收费模式", "收费标准", "价格表", "报价单", "多少钱一个",
            "价格多少", "价格怎么样", "价格合适吗", "价格能不能谈",
            "给个价", "报个价", "最终价格", "成交价", "底价",
            "市场价", "指导价", "建议零售价", "实际价格", "到手价",
            "性价比", "划算", "值不值", "贵不贵", "价格合理",
        ],
        "complaint": [
            "投诉", "退款", "退货", "差评", "不满", "举报", "抱怨",
            "质量差", "服务差", "态度差", "欺骗", "虚假", "骗人",
            "不靠谱", "不可信", "失望", "后悔", "不值", "亏了",
            "被坑", "被忽悠", "被欺骗", "要求退款", "要退货",
            "换货", "赔偿", "补偿", "维权", "曝光", "差评",
            "给差评", "投诉你们", "举报你们", "找消协", "315",
            "质量问题", "与描述不符", "货不对板", "假冒伪劣",
            "虚假宣传", "夸大宣传", "误导消费", "消费欺诈",
            "服务态度恶劣", "客服不专业", "处理太慢", "没人理",
            "推卸责任", "不解决问题", "扯皮", "踢皮球",
            "要求赔偿", "精神损失", "耽误事", "浪费时间"
        ],
        "demo_request": [
            "试用", "体验", "演示", "看看效果", "试一下", "试用一下",
            "免费试用", "试用版", "体验版", "demo", "演示一下",
            "看下效果", "先试试", "测试一下", "测试版", "样机",
            "样品", "试用账号", "试用权限", "开通试用", "申请试用",
            "试用申请", "试用期限", "试用功能", "试用范围",
            "免费体验", "体验账号", "体验中心", "在线演示",
            "预约演示", "上门演示", "远程演示", "视频演示",
            "功能演示", "效果演示", "案例演示", "实际操作",
            "先体验再买", "先试用再决定", "试用满意再付款"
        ],
        "contact_intent": [
            "联系", "电话", "微信", "加微信", "留电话", "联系方式",
            "手机号", "微信号", "QQ", "邮箱", "地址", "加好友",
            "加你", "私聊", "私信", "留个联系方式", "怎么联系",
            "能留个电话吗", "能加微信吗", "方便联系吗", "回电话",
            "电话沟通", "语音沟通", "视频沟通", "面谈", "见面聊",
            "上门拜访", "约时间", "约个时间", "方便的时候",
            "工作时间", "非工作时间", "紧急联系", "商务联系",
            "技术联系", "售后联系", "客服电话", "服务热线",
            "400电话", "官方微信", "官方QQ", "客服邮箱"
        ],
        "comparison": [
            "对比", "比较", "区别", "哪个好", "优缺点", "差异",
            "不一样", "有什么不同", "哪个更好", "哪个合适",
            "怎么选", "选择哪个", "对比一下", "比较一下",
            "和...比", "相比", "对比其他", "竞品对比", "横向对比",
            "纵向对比", "功能对比", "价格对比", "性能对比",
            "优势对比", "劣势对比", "参数对比", "配置对比",
            "性价比对比", "服务对比", "品牌对比", "口碑对比",
            "用户评价对比", "市场占有率对比", "行业排名",
            "同类产品", "竞品分析", "替代方案", "备选方案"
        ],
        "timeline": [
            "什么时候", "多久", "几天", "什么时候能", "需要多长时间",
            "多长时间", "什么时候可以", "什么时候发货", "什么时候到",
            "几天到", "多久能到", "什么时候能用", "什么时候开始",
            "什么时候完成", "工期", "周期", "时间", "日期",
            "交期", "交货期", "交付时间", "到货时间", "发货时间",
            "处理时间", "响应时间", "上线时间", "部署时间",
            "实施周期", "开发周期", "测试周期", "验收时间",
            "合同期限", "服务期限", "有效期", "截止日期",
            "最晚时间", "最早时间", "预计时间", "大概时间"
        ],
        "quantity": [
            "多少个", "几个", "数量", "批量", "大量", "多少量",
            "起订量", "最小起订", "批量购买", "批发数量",
            "库存", "有货吗", "有现货吗", "够不够", "有多少",
            "库存多少", "现货数量", "备货", "备货量", "库存情况",
            "缺货", "断货", "补货时间", "预售", "限量",
            "限购", "最大购买量", "最小购买量", "购买限制",
            "供应能力", "产能", "日产量", "月产量", "年产量",
            "供货周期", "供货能力", "稳定供货", "长期供货"
        ],
        "feature_inquiry": [
            "功能", "特性", "特点", "功能介绍", "功能详情",
            "有什么功能", "能做什么", "支持什么", "功能列表",
            "核心功能", "主要功能", "基础功能", "高级功能",
            "特色功能", "独家功能", "创新功能", "实用功能",
            "功能演示", "功能说明", "功能详解", "功能对比",
            "功能需求", "定制功能", "扩展功能", "插件功能",
            "API接口", "二次开发", "集成能力", "兼容性"
        ],
        "service_inquiry": [
            "服务", "售后服务", "技术支持", "客服", "服务内容",
            "服务范围", "服务条款", "服务协议", "服务承诺",
            "服务标准", "服务质量", "服务响应", "服务时效",
            "上门服务", "远程服务", "现场服务", "驻场服务",
            "培训服务", "实施服务", "运维服务", "升级服务",
            "维护服务", "保修服务", "延保服务", "VIP服务",
            "专属服务", "一对一服务", "7x24服务", "节假日服务"
        ],
        "security_inquiry": [
            "安全", "数据安全", "信息安全", "隐私保护", "数据保护",
            "保密", "加密", "数据加密", "传输加密", "存储加密",
            "数据备份", "容灾", "灾备", "数据恢复", "数据迁移",
            "权限管理", "访问控制", "身份认证", "安全认证",
            "合规", "资质", "ISO认证", "等保", "安全等级",
            "数据泄露", "隐私政策", "保密协议", "NDA"
        ],
        "payment_inquiry": [
            "付款", "支付", "付款方式", "支付方式", "怎么付款",
            "怎么支付", "在线支付", "银行转账", "对公转账",
            "支付宝", "微信支付", "银联支付", "信用卡支付",
            "分期付款", "分期支付", "首付", "尾款", "全款",
            "定金", "预付款", "货到付款", "账期", "月结",
            "发票", "开票", "增值税发票", "普通发票", "专用发票",
            "发票抬头", "税号", "收据", "凭证"
        ],
        "delivery_inquiry": [
            "发货", "配送", "快递", "物流", "送货", "发货方式",
            "配送方式", "快递公司", "物流公司", "运费", "包邮",
            "到付", "自提", "送货上门", "指定地点", "代收货款",
            "物流查询", "快递查询", "物流跟踪", "发货通知",
            "签收", "验货", "收货确认", "配送范围", "配送时间"
        ],
        "contract_inquiry": [
            "合同", "协议", "签约", "合同条款", "协议内容",
            "服务协议", "购买合同", "销售合同", "代理合同",
            "合作协议", "保密协议", "框架协议", "补充协议",
            "合同期限", "合同续签", "合同变更", "合同解除",
            "违约责任", "争议解决", "法律效力", "盖章", "签字"
        ],
        "upgrade_inquiry": [
            "升级", "更新", "版本升级", "功能更新", "系统升级",
            "新版本", "最新版", "版本号", "更新内容", "升级内容",
            "免费升级", "付费升级", "自动升级", "手动升级",
            "升级费用", "升级政策", "版本对比", "版本历史",
            "更新日志", "发布说明", "新功能", "功能优化"
        ],
        "account_inquiry": [
            "账号", "账户", "登录", "注册", "账号问题",
            "登录问题", "注册问题", "密码", "密码找回", "密码重置",
            "账号注销", "账号冻结", "账号解冻", "账号安全",
            "账号绑定", "账号解绑", "手机绑定", "邮箱绑定",
            "实名认证", "企业认证", "资质认证", "认证问题"
        ]
    }
    
    def __init__(self, context_module=None):
        self._context_module = context_module or context_understanding_module
        self.rule_weights = {
            "明确合作意向": 40,
            "价格咨询": 15,
            "高互动频率": 20,
            "快速响应": 10,
            "留联系方式": 30,
            "负面情绪": -15,
            "投诉表达": -20
        }

        self.CONVERSION_SIGNAL_KEYWORDS = {
            "date_inquiry": ["什么时候", "哪天", "几号", "多久", "周期", "交期", "日期", "明天", "后天", "下周", "下个月", "出发"],
            "quantity_inquiry": ["多少个", "几个", "多少量", "批量", "起订量"],
            "price_quote": ["报价", "报个价", "价格表", "多少钱", "总共多少", "优惠", "折扣", "给个价"],
            "detail_inquiry": ["具体内容", "详细说明", "功能介绍", "参数", "配置", "规格"],
            "stock_availability": ["有货吗", "有现货吗", "库存", "够不够", "能否供货", "位置", "余位", "名额", "档期", "能安排", "还能不能安排", "还有没有位置", "还有位置"],
            "purchase_intent": ["下单", "订购", "预订", "付款", "签约", "成交", "确定要"],
            "decision_urgency": ["马上买", "现在买", "立即下单", "今天就要", "尽快安排"],
            "contact_signal": ["联系", "联系方式", "电话", "加微信", "留电话", "怎么联系你"],
        }
        self._domain_signal_keywords = self._load_domain_signal_keywords()

        self.ASSISTANT_STYLE_MARKERS = [
            "亲爱的", "期待为您服务", "有需要随时找我", "还有什么需要帮助",
            "对公转账需要", "转账凭证序号", "方便的话留个接收资料的联系方式",
            "我把说明发您", "我把完整说明发您",
        ]
        self.INBOUND_BUSINESS_WHITELIST = [
            "日期", "时间", "人数", "预约",
            "价格", "费用", "预算", "报价", "产品", "服务", "方案", "资料", "联系", "联系方式",
            "微信", "电话", "套餐",
        ]
        self._domain_business_whitelist = self._load_domain_business_whitelist()

        self.REJECTION_KEYWORDS = {
            RejectionType.PRICE: [
                "太贵了", "价格太高", "买不起", "超出预算", "太贵",
                "价格不合适", "有点贵", "价格接受不了", "负担不起",
                "比别的贵", "别家便宜", "价格没优势", "性价比不高"
            ],
            RejectionType.TIMING: [
                "以后再说", "暂时不需要", "现在不急", "过段时间",
                "等一等", "再看看", "不着急", "以后再考虑",
                "目前不需要", "现在不方便", "过阵子", "缓一缓"
            ],
            RejectionType.COMPETITOR: [
                "已经买了", "已经定了", "选了别家", "用了其他",
                "在别家买了", "有合作方了", "已经签约了", "定下来了",
                "和别人合作了", "选了竞品", "用了竞品"
            ],
            RejectionType.NOT_INTERESTED: [
                "不感兴趣", "不需要", "不想买", "没兴趣",
                "不想了解", "不用了", "没必要", "用不上",
                "不适合", "不是我想要的", "不符合需求"
            ],
            RejectionType.BUDGET: [
                "没预算", "预算不够", "预算不足", "没有经费",
                "资金紧张", "没钱", "经费有限", "预算已用完",
                "超出预算", "预算被砍"
            ],
            RejectionType.AUTHORITY: [
                "做不了主", "需要请示", "要问领导", "我说了不算",
                "需要审批", "要汇报", "没有决策权", "需要商量",
                "要和老板说", "需要开会讨论"
            ],
            RejectionType.NEED: [
                "没有需求", "用不着", "不需要这个", "没有这方面需求",
                "目前没需求", "暂时用不上", "没这个必要"
            ]
        }
        
        self.INTENT_STRENGTH_KEYWORDS = {
            IntentStrength.VERY_STRONG: [
                "现在就要", "马上买", "立即下单", "今天定", "马上付款",
                "现在付款", "马上订购", "立即购买", "现在订购", "马上签约",
                "今天就要", "现在就买", "立刻要", "马上成交"
            ],
            IntentStrength.STRONG: [
                "想买", "要买", "准备买", "打算买", "确定要",
                "想订购", "要订购", "想下单", "要下单", "决定买",
                "考虑买", "准备订购", "准备下单", "想要", "有兴趣购买"
            ],
            IntentStrength.MODERATE: [
                "了解一下", "咨询一下", "问问", "看看", "了解一下",
                "想了解", "想咨询", "打听一下", "问一下", "看看怎么样"
            ],
            IntentStrength.WEAK: [
                "随便看看", "随便问问", "只是问问", "了解一下而已",
                "先看看", "先了解一下", "暂时看看", "随便了解"
            ]
        }
        
        self.INTENT_TYPE_KEYWORDS = {
            PurchaseIntentType.PURCHASE: [
                "买", "购买", "下单", "付款", "订购", "成交", "支付"
            ],
            PurchaseIntentType.INQUIRY: [
                "咨询", "了解", "问问", "请问", "打听", "询问"
            ],
            PurchaseIntentType.COMPARISON: [
                "对比", "比较", "区别", "哪个好", "哪个更好", "对比一下",
                "有什么不同", "区别在哪", "优缺点"
            ],
            PurchaseIntentType.COOPERATION: [
                "合作", "加盟", "代理", "经销", "分销", "合伙"
            ],
            PurchaseIntentType.COMPLAINT: [
                "投诉", "举报", "差评", "不满", "退款", "退货"
            ],
            PurchaseIntentType.SUPPORT: [
                "售后", "维修", "问题", "故障", "报错", "客服"
            ],
            PurchaseIntentType.DEMO_REQUEST: [
                "试用", "体验", "演示", "demo", "免费试用", "先试试"
            ],
            PurchaseIntentType.CONTACT: [
                "联系", "微信", "电话", "手机", "加微信", "留电话"
            ],
            PurchaseIntentType.INFORMATION: [
                "是什么", "做什么的", "有什么用", "功能", "介绍", "说明"
            ]
        }

    def _load_domain_signal_keywords(self, enterprise_id: str = "") -> dict:
        try:
            from src.common.industry_schema_service import get_industry_schema_service
            schema = get_industry_schema_service().get_active_schema(enterprise_id=enterprise_id) or {}
            metadata = schema.get("metadata") or {}
            fs_config = metadata.get("followup_strategy") or {}
            return fs_config.get("signal_terms") or {}
        except Exception:
            return {}

    def _load_domain_business_whitelist(self, enterprise_id: str = "") -> list:
        try:
            from src.common.industry_schema_service import get_industry_schema_service
            schema = get_industry_schema_service().get_active_schema(enterprise_id=enterprise_id) or {}
            metadata = schema.get("metadata") or {}
            fs_config = metadata.get("followup_strategy") or {}
            return fs_config.get("domain_keywords") or []
        except Exception:
            return []

    def _resolve_context_key(self, customer_data: Dict[str, Any]) -> str:
        platform = str(customer_data.get("platform") or "douyin").strip() or "douyin"
        customer_id = str(
            customer_data.get("sec_uid")
            or customer_data.get("customer_id")
            or customer_data.get("nickname")
            or ""
        ).strip()
        return resolve_context_session_id(
            platform=platform,
            customer_id=customer_id,
            provided_session_id=customer_data.get("session_id"),
            conversation_history=None,
        ) or platform

    def _sync_shared_context(self, customer_data: Dict[str, Any], message_history: List[Dict] = None):
        context_key = self._resolve_context_key(customer_data)
        customer_id = str(
            customer_data.get("sec_uid")
            or customer_data.get("customer_id")
            or customer_data.get("nickname")
            or context_key
        ).strip() or context_key
        return self._context_module.hydrate_history(context_key, customer_id, message_history)

    def _looks_like_assistant_message(self, content: str) -> bool:
        text = (content or "").strip().lower()
        if not text:
            return True
        if any(marker.lower() in text for marker in self.ASSISTANT_STYLE_MARKERS):
            return True
        if text.startswith(("亲爱的", "您好", "您好！")) and ("服务" in text or "发您" in text):
            return True
        return False

    def _should_preserve_inbound_business_message(self, content: str, enterprise_id: str = "") -> bool:
        text = str(content or "").strip().lower()
        if not text:
            return False
        matched = [
            keyword.lower()
            for keyword in self.INBOUND_BUSINESS_WHITELIST
            if keyword.lower() in text
        ]
        strong_signals = {
            "日期", "人数", "预约",
            "价格", "费用", "预算", "报价", "微信", "电话",
        }
        dynamic_business_whitelist = self._load_domain_business_whitelist(enterprise_id) or self._domain_business_whitelist
        strong_signals.update(dynamic_business_whitelist)
        return len(set(matched)) >= 2 or any(signal in text for signal in strong_signals)

    def _get_recent_messages(
        self,
        message_history: List[Dict] = None,
        limit: int = 5,
        *,
        customer_only: bool = False,
        enterprise_id: str = "",
    ) -> List[Dict]:
        """统一提取最近消息，避免把旧消息或明显的助手话术当成客户上下文。"""
        if not message_history:
            return []

        messages = [msg for msg in message_history if isinstance(msg, dict) and (msg.get("content") or "").strip()]
        if not messages:
            return []

        recent = messages[-limit:] if len(messages) > limit else list(messages)
        if not customer_only:
            return recent

        filtered = []
        for msg in recent:
            direction = (msg.get("direction") or "").lower().strip()
            content = msg.get("content", "")
            if direction == "outbound":
                continue
            if self._should_preserve_inbound_business_message(content, enterprise_id=enterprise_id):
                filtered.append(msg)
                continue
            if self._looks_like_assistant_message(content):
                continue
            filtered.append(msg)
        return filtered
    
    def analyze(self, customer_data: Dict, message_history: List[Dict] = None) -> IntentAnalysisResult:
        """
        分析客户意向
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            IntentAnalysisResult: 分析结果
        """
        customer_id = customer_data.get("sec_uid", "")
        platform = customer_data.get("platform", "douyin")
        enterprise_id = str(customer_data.get("enterprise_id") or "").strip()
        self._sync_shared_context(customer_data, message_history)
        
        behavior_score = self._calculate_behavior_score(customer_data, message_history)
        semantic_score = self._calculate_semantic_score(customer_data, message_history)
        interaction_score = self._calculate_interaction_score(customer_data, message_history)
        
        emotion_score, emotion_type = self._analyze_emotion(customer_data, message_history)
        urgency_score, urgency_level = self._analyze_urgency(customer_data, message_history)
        stage_score, purchase_stage = self._analyze_purchase_stage(customer_data, message_history)
        
        context_trend = self._analyze_context_trend(message_history)
        conversion_signals = self._collect_conversion_signals(customer_data, message_history)
        
        rejection_info = self._detect_rejection(customer_data, message_history)
        primary_intent, secondary_intents = self._analyze_intent_types(customer_data, message_history)
        intent_strength = self._analyze_intent_strength(customer_data, message_history)
        
        total_score = (
            behavior_score * self.WEIGHTS["behavior"] +
            semantic_score * self.WEIGHTS["semantic"] +
            interaction_score * self.WEIGHTS["interaction"] +
            emotion_score * 0.15 +
            urgency_score * 0.20 +
            stage_score * 0.10
        )
        
        if rejection_info:
            total_score *= 0.5
        
        if context_trend["trend"] == "improving":
            total_score *= 1.15
        elif context_trend["trend"] == "declining":
            total_score *= 0.85

        total_score += min(len(conversion_signals) * 3.5, 14)
        
        total_score = self._apply_time_decay(total_score, message_history)
        
        level = self._map_to_level(total_score)
        
        confidence = self._calculate_confidence(
            behavior_score, semantic_score, interaction_score,
            emotion_score, urgency_score, stage_score,
            primary_intent, rejection_info
        )
        
        score = IntentScore(
            total_score=total_score,
            behavior_score=behavior_score,
            semantic_score=semantic_score,
            interaction_score=interaction_score,
            emotion_score=emotion_score,
            urgency_score=urgency_score,
            stage_score=stage_score,
            confidence=confidence
        )
        
        factors = {
            "behavior_factors": self._get_behavior_factors(customer_data),
            "semantic_factors": self._get_semantic_factors(message_history),
            "interaction_factors": self._get_interaction_factors(message_history),
            "emotion_type": emotion_type.value if emotion_type else "neutral",
            "urgency_level": urgency_level.value if urgency_level else "low",
            "purchase_stage": purchase_stage.value if purchase_stage else "awareness",
            "context_trend": context_trend["trend"],
            "topic_shifts": context_trend["topic_shifts"],
            "intent_strength": intent_strength.value if intent_strength else "none",
            "conversion_signals": conversion_signals,
        }
        
        predicted_action = self._predict_next_action(
            level,
            primary_intent,
            purchase_stage,
            urgency_level,
            conversion_signals,
        )
        recommended_response = self._recommend_response_type(
            level,
            primary_intent,
            rejection_info,
            intent_strength,
            conversion_signals,
        )
        
        result = IntentAnalysisResult(
            customer_id=customer_id,
            platform=platform,
            level=level,
            score=score,
            factors=factors,
            primary_intent=primary_intent,
            secondary_intents=secondary_intents,
            rejection_info=rejection_info,
            predicted_next_action=predicted_action,
            recommended_response_type=recommended_response
        )
        
        logger.info(f"意向分析结果: {customer_id} - {level.value} ({total_score:.1f}分) 意图:{primary_intent.intent_type.value if primary_intent else 'none'} 强度:{intent_strength.value if intent_strength else 'none'} 置信度:{confidence:.2f}")
        
        return result

    def _collect_conversion_signals(self, customer_data: Dict, message_history: List[Dict] = None) -> List[str]:
        """提取当前会话中的高转化信号，用于抬升高意向判定。"""
        contents = []
        comment_content = (customer_data.get("comment_content", "") or "").strip()
        if comment_content:
            contents.append(comment_content.lower())
        enterprise_id = str(customer_data.get("enterprise_id") or "").strip()
        for msg in self._get_recent_messages(message_history, limit=6, customer_only=True, enterprise_id=enterprise_id):
            content = (msg.get("content", "") or "").strip().lower()
            if content:
                contents.append(content)

        if not contents:
            return []

        merged_text = " ".join(contents)
        detected = []
        compatibility_aliases = {
            "date_inquiry": "travel_date",
            "stock_availability": "availability_check",
        }
        for signal_name, keywords in self.CONVERSION_SIGNAL_KEYWORDS.items():
            if any(keyword in merged_text for keyword in keywords):
                normalized = compatibility_aliases.get(signal_name, signal_name)
                if normalized not in detected:
                    detected.append(normalized)
        return detected
    
    def _calculate_behavior_score(self, customer_data: Dict, message_history: List[Dict] = None) -> float:
        """计算行为维度评分"""
        score = 0
        
        status = customer_data.get("status", "")
        if status == "replied":
            score += 20
        
        tags = customer_data.get("tags", [])
        if isinstance(tags, list):
            if "high_activity" in tags:
                score += 20
            elif "medium_activity" in tags:
                score += 10
        
        if message_history:
            score += min(len(message_history) * 5, 20)
        
        return min(score, 100)
    
    def _calculate_semantic_score(self, customer_data: Dict, message_history: List[Dict] = None) -> float:
        """计算语义维度评分"""
        score = 0
        
        comment_content = customer_data.get("comment_content", "")
        
        matched_intents = set()
        for intent_type, keywords in self.INTENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in comment_content:
                    matched_intents.add(intent_type)
                    break
        
        for intent_type in matched_intents:
            if intent_type in ["cooperation", "purchase"]:
                score += 30
            elif intent_type == "price":
                score += 15
            elif intent_type in ["demo_request", "contact_intent"]:
                score += 20
            elif intent_type == "complaint":
                score -= 20
        
        enterprise_id = str(customer_data.get("enterprise_id") or "").strip()
        recent_customer_messages = self._get_recent_messages(
            message_history,
            limit=8,
            customer_only=True,
            enterprise_id=enterprise_id,
        )
        if recent_customer_messages:
            all_content = " ".join([
                msg.get("content", "") for msg in recent_customer_messages
            ])
            
            matched_intents_history = set()
            high_intent_count = 0
            
            for intent_type, keywords in self.INTENT_KEYWORDS.items():
                for keyword in keywords:
                    if keyword in all_content:
                        matched_intents_history.add(intent_type)
                        if intent_type in ["cooperation", "purchase", "demo_request", "contact_intent"]:
                            high_intent_count += 1
                        break
            
            for intent_type in matched_intents_history:
                if intent_type in ["cooperation", "purchase"]:
                    score += 25 + min(high_intent_count * 5, 15)
                elif intent_type == "price":
                    score += 15
                elif intent_type in ["demo_request", "contact_intent"]:
                    score += 20
                elif intent_type == "complaint":
                    score -= 15
        
        return max(0, min(score, 100))
    
    def _calculate_interaction_score(self, customer_data: Dict, message_history: List[Dict] = None) -> float:
        """计算交互维度评分"""
        score = 0
        
        if message_history:
            inbound_count = sum(1 for msg in message_history if msg.get("direction") == "inbound")
            outbound_count = sum(1 for msg in message_history if msg.get("direction") == "outbound")
            
            if inbound_count > 0:
                reply_rate = outbound_count / inbound_count
                score += min(reply_rate * 30, 30)
        
        tags = customer_data.get("tags", [])
        if isinstance(tags, list):
            if "contact_shared" in tags:
                score += 40
            elif "wechat_shared" in tags:
                score += 50
            elif "phone_shared" in tags:
                score += 50
        
        if isinstance(tags, list):
            if "complaint" in tags:
                score -= 30
            if "blacklist" in tags:
                score = 0
        
        return max(0, min(score, 100))
    
    def _map_to_level(self, score: float) -> IntentLevel:
        """分数映射到等级"""
        if score >= self.LEVEL_THRESHOLDS[IntentLevel.A]:
            return IntentLevel.A
        elif score >= self.LEVEL_THRESHOLDS[IntentLevel.B]:
            return IntentLevel.B
        elif score >= self.LEVEL_THRESHOLDS[IntentLevel.C]:
            return IntentLevel.C
        elif score >= self.LEVEL_THRESHOLDS[IntentLevel.D]:
            return IntentLevel.D
        else:
            return IntentLevel.E
    
    def _get_behavior_factors(self, customer_data: Dict) -> Dict:
        """获取行为因素"""
        return {
            "status": customer_data.get("status", ""),
            "tags": customer_data.get("tags", [])
        }
    
    def _get_semantic_factors(self, message_history: List[Dict] = None) -> Dict:
        """获取语义因素"""
        if not message_history:
            return {"keywords": [], "intents": []}
        
        recent_customer_messages = self._get_recent_messages(message_history, limit=8, customer_only=True)
        all_content = " ".join([
            msg.get("content", "") for msg in recent_customer_messages
        ])
        
        detected_keywords = []
        detected_intents = []
        
        for intent_type, keywords in self.INTENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in all_content:
                    detected_keywords.append(keyword)
                    detected_intents.append(intent_type)
        
        return {
            "keywords": list(set(detected_keywords)),
            "intents": list(set(detected_intents))
        }
    
    def _get_interaction_factors(self, message_history: List[Dict] = None) -> Dict:
        """获取交互因素"""
        if not message_history:
            return {"message_count": 0, "reply_rate": 0}
        
        inbound_count = sum(1 for msg in message_history if msg.get("direction") == "inbound")
        outbound_count = sum(1 for msg in message_history if msg.get("direction") == "outbound")
        
        reply_rate = outbound_count / inbound_count if inbound_count > 0 else 0
        
        return {
            "message_count": len(message_history),
            "inbound_count": inbound_count,
            "outbound_count": outbound_count,
            "reply_rate": reply_rate
        }
    
    def _analyze_emotion(self, customer_data: Dict, message_history: List[Dict] = None) -> tuple:
        """
        分析客户情感状态
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            tuple: (情感分数, 情感类型)
        """
        emotion_keywords = {
            EmotionType.POSITIVE: [
                "太好了", "很好", "不错", "喜欢", "满意", "感谢", "谢谢", "棒", "赞",
                "太棒了", "非常好", "很满意", "很棒", "很赞", "完美", "优秀",
                "期待", "希望", "感兴趣", "心动", "想要", "想买", "想了解",
                "可以", "行", "好的", "没问题", "确定", "成交", "下单"
            ],
            EmotionType.NEGATIVE: [
                "不好", "不行", "差", "烂", "垃圾", "骗子", "欺骗", "投诉",
                "退款", "退货", "不满", "失望", "后悔", "差评", "举报",
                "太贵", "太差", "质量差", "服务差", "态度差", "骗人"
            ],
            EmotionType.ANXIOUS: [
                "急", "着急", "快点", "尽快", "什么时候", "等多久", "多久",
                "担心", "怕", "会不会", "能不能", "有没有问题", "可靠吗",
                "安全吗", "靠谱吗", "真的吗", "确定吗", "保证", "承诺"
            ],
            EmotionType.EXCITED: [
                "哇", "太棒", "太好了", "太赞了", "太喜欢了", "太满意了",
                "终于", "等不及", "迫不及待", "马上", "立即", "现在就要",
                "一定要", "必须", "非你不可", "就这个了"
            ]
        }
        
        emotion_scores = {emotion: 0 for emotion in EmotionType}
        
        if message_history:
            enterprise_id = str(customer_data.get("enterprise_id") or "").strip()
            recent_messages = self._get_recent_messages(
                message_history,
                limit=5,
                customer_only=True,
                enterprise_id=enterprise_id,
            )
            for msg in recent_messages:
                content = msg.get("content", "").lower()
                for emotion, keywords in emotion_keywords.items():
                    for keyword in keywords:
                        if keyword in content:
                            emotion_scores[emotion] += 1
        
        max_emotion = max(emotion_scores.items(), key=lambda x: x[1])
        
        if max_emotion[1] == 0:
            return 0, EmotionType.NEUTRAL
        
        emotion_type = max_emotion[0]
        emotion_score = min(max_emotion[1] * 5, 20)
        
        if emotion_type == EmotionType.POSITIVE:
            emotion_score = emotion_score
        elif emotion_type == EmotionType.NEGATIVE:
            emotion_score = -emotion_score
        elif emotion_type == EmotionType.EXCITED:
            emotion_score = emotion_score * 1.5
        elif emotion_type == EmotionType.ANXIOUS:
            emotion_score = emotion_score * 0.5
        
        return emotion_score, emotion_type
    
    def _analyze_urgency(self, customer_data: Dict, message_history: List[Dict] = None) -> tuple:
        """
        分析客户紧迫程度
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            tuple: (紧迫分数, 紧迫等级)
        """
        high_urgency_keywords = [
            "马上", "立即", "现在", "今天", "马上要", "急需", "急用",
            "等不及", "越快越好", "立刻", "马上买", "现在就要", "今天就",
            "明天要", "这周要", "马上付款", "立即付款", "现在付款"
        ]
        
        medium_urgency_keywords = [
            "这周", "下周", "最近", "尽快", "早点", "想快点",
            "什么时候能", "多久能", "几天能", "什么时候发货"
        ]
        
        low_urgency_keywords = [
            "以后", "再说", "先看看", "了解一下", "咨询一下",
            "考虑一下", "想想", "对比一下", "再看看"
        ]
        
        urgency_score = 0
        
        if message_history:
            enterprise_id = str(customer_data.get("enterprise_id") or "").strip()
            recent_messages = self._get_recent_messages(
                message_history,
                limit=3,
                customer_only=True,
                enterprise_id=enterprise_id,
            )
            for msg in recent_messages:
                content = msg.get("content", "").lower()
                
                for keyword in high_urgency_keywords:
                    if keyword in content:
                        urgency_score += 15
                
                for keyword in medium_urgency_keywords:
                    if keyword in content:
                        urgency_score += 8
                
                for keyword in low_urgency_keywords:
                    if keyword in content:
                        urgency_score -= 5
        
        urgency_score = max(-10, min(30, urgency_score))
        
        if urgency_score >= 20:
            return urgency_score, UrgencyLevel.HIGH
        elif urgency_score >= 8:
            return urgency_score, UrgencyLevel.MEDIUM
        else:
            return urgency_score, UrgencyLevel.LOW
    
    def _analyze_purchase_stage(self, customer_data: Dict, message_history: List[Dict] = None) -> tuple:
        """
        分析客户购买阶段
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            tuple: (阶段分数, 购买阶段)
        """
        stage_keywords = {
            PurchaseStage.AWARENESS: [
                "是什么", "做什么的", "有什么用", "能做什么", "介绍",
                "了解", "咨询", "问问", "看看", "了解一下"
            ],
            PurchaseStage.INTEREST: [
                "功能", "特点", "优势", "对比", "区别", "哪个好",
                "怎么用", "怎么操作", "教程", "演示", "试用"
            ],
            PurchaseStage.CONSIDERATION: [
                "价格", "多少钱", "费用", "优惠", "折扣", "活动",
                "性价比", "值不值", "划算", "预算", "报价", "费用明细"
            ],
            PurchaseStage.INTENT: [
                "买", "订购", "下单", "付款", "支付", "购买",
                "要了", "定了", "成交", "签约"
            ],
            PurchaseStage.DECISION: [
                "现在买", "马上付款", "立即下单", "今天定", "怎么付款",
                "付款方式", "发票", "合同", "协议", "马上买", "立即购买",
                "现在就要", "今天就买", "马上订购"
            ]
        }
        
        stage_weights = {
            PurchaseStage.AWARENESS: 1,
            PurchaseStage.INTEREST: 2,
            PurchaseStage.CONSIDERATION: 3,
            PurchaseStage.INTENT: 4,
            PurchaseStage.DECISION: 5
        }
        
        stage_scores = {stage: 0 for stage in PurchaseStage}
        
        if message_history:
            recent_messages = self._get_recent_messages(message_history, limit=5, customer_only=True)
            for msg in recent_messages:
                content = msg.get("content", "").lower()
                for stage, keywords in stage_keywords.items():
                    for keyword in keywords:
                        if keyword in content:
                            stage_scores[stage] += stage_weights[stage]
        
        max_stage = max(stage_scores.items(), key=lambda x: x[1])
        
        if max_stage[1] == 0:
            if customer_data.get("status") == "replied":
                return 5, PurchaseStage.INTEREST
            return 0, PurchaseStage.AWARENESS
        
        stage_score = min(max_stage[1] * 3, 25)
        return stage_score, max_stage[0]
    
    def _analyze_context_trend(self, message_history: List[Dict] = None) -> Dict:
        """
        分析对话意图变化趋势
        
        Args:
            message_history: 消息历史
            
        Returns:
            Dict: 趋势分析结果
        """
        if not message_history or len(message_history) < 2:
            return {
                "trend": "stable",
                "intent_progression": 0,
                "topic_shifts": 0
            }
        
        intent_progression = 0
        topic_shifts = 0
        prev_intents = set()
        
        recent_messages = self._get_recent_messages(message_history, limit=10, customer_only=True)
        for i, msg in enumerate(reversed(recent_messages)):
            content = msg.get("content", "").lower()
            current_intents = set()
            
            for intent_type, keywords in self.INTENT_KEYWORDS.items():
                for keyword in keywords:
                    if keyword in content:
                        current_intents.add(intent_type)
                        break
            
            if i > 0:
                if current_intents and not current_intents.intersection(prev_intents):
                    topic_shifts += 1
                
                high_intent_types = {"purchase", "cooperation", "demo_request", "contact_intent"}
                if current_intents.intersection(high_intent_types) and not prev_intents.intersection(high_intent_types):
                    intent_progression += 1
                elif not current_intents.intersection(high_intent_types) and prev_intents.intersection(high_intent_types):
                    intent_progression -= 1
            
            prev_intents = current_intents
        
        if intent_progression >= 2:
            trend = "improving"
        elif intent_progression <= -2:
            trend = "declining"
        else:
            trend = "stable"
        
        return {
            "trend": trend,
            "intent_progression": intent_progression,
            "topic_shifts": topic_shifts
        }
    
    def _apply_time_decay(self, score: float, message_history: List[Dict] = None) -> float:
        """
        应用时间衰减机制
        
        Args:
            score: 原始分数
            message_history: 消息历史
            
        Returns:
            float: 衰减后的分数
        """
        if not message_history:
            return score * 0.5
        
        try:
            recent_messages = self._get_recent_messages(message_history, limit=1, customer_only=True) or self._get_recent_messages(message_history, limit=1)
            last_msg = recent_messages[-1]
            last_time_str = last_msg.get("timestamp") or last_msg.get("created_at")
            
            if last_time_str:
                if isinstance(last_time_str, str):
                    last_time = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
                else:
                    last_time = last_time_str
                
                hours_since_last = (datetime.now() - last_time).total_seconds() / 3600
                
                if hours_since_last < 1:
                    decay_factor = 1.0
                elif hours_since_last < 24:
                    decay_factor = 0.9
                elif hours_since_last < 72:
                    decay_factor = 0.7
                elif hours_since_last < 168:
                    decay_factor = 0.5
                else:
                    decay_factor = 0.3
                
                return score * decay_factor
        except Exception as e:
            logger.warning(f"时间衰减计算失败: {e}")
        
        return score
    
    def _detect_rejection(self, customer_data: Dict, message_history: List[Dict] = None) -> Optional[Dict]:
        """
        检测拒绝意图
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            Optional[Dict]: 拒绝信息，无拒绝返回None
        """
        if not message_history:
            return None
        
        recent_messages = self._get_recent_messages(message_history, limit=3, customer_only=True)
        all_content = " ".join([msg.get("content", "").lower() for msg in recent_messages])
        
        detected_rejections = []
        
        for rejection_type, keywords in self.REJECTION_KEYWORDS.items():
            matched_keywords = []
            for keyword in keywords:
                if keyword in all_content:
                    matched_keywords.append(keyword)
            
            if matched_keywords:
                detected_rejections.append({
                    "type": rejection_type.value,
                    "keywords_matched": matched_keywords,
                    "strength": len(matched_keywords)
                })
        
        if detected_rejections:
            detected_rejections.sort(key=lambda x: x["strength"], reverse=True)
            return detected_rejections[0]
        
        return None
    
    def _analyze_intent_types(self, customer_data: Dict, message_history: List[Dict] = None) -> Tuple[Optional[IntentDetail], List[IntentDetail]]:
        """
        分析意图类型
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            Tuple: (主要意图, 次要意图列表)
        """
        if not message_history:
            return None, []
        
        recent_messages = self._get_recent_messages(message_history, limit=5, customer_only=True)
        all_content = " ".join([msg.get("content", "").lower() for msg in recent_messages])
        
        intent_scores = {}
        
        for intent_type, keywords in self.INTENT_TYPE_KEYWORDS.items():
            matched_keywords = []
            for keyword in keywords:
                if keyword in all_content:
                    matched_keywords.append(keyword)
            
            if matched_keywords:
                intent_scores[intent_type] = {
                    "score": len(matched_keywords) * 10,
                    "keywords": matched_keywords
                }
        
        if not intent_scores:
            return None, []
        
        sorted_intents = sorted(intent_scores.items(), key=lambda x: x[1]["score"], reverse=True)
        
        primary_intent = IntentDetail(
            intent_type=sorted_intents[0][0],
            strength=self._score_to_strength(sorted_intents[0][1]["score"]),
            score=sorted_intents[0][1]["score"],
            keywords_matched=sorted_intents[0][1]["keywords"],
            confidence=min(sorted_intents[0][1]["score"] / 30, 1.0)
        )
        
        secondary_intents = []
        for intent_type, data in sorted_intents[1:3]:
            secondary_intents.append(IntentDetail(
                intent_type=intent_type,
                strength=self._score_to_strength(data["score"]),
                score=data["score"],
                keywords_matched=data["keywords"],
                confidence=min(data["score"] / 30, 1.0)
            ))
        
        return primary_intent, secondary_intents
    
    def _analyze_intent_strength(self, customer_data: Dict, message_history: List[Dict] = None) -> Optional[IntentStrength]:
        """
        分析意图强度
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            IntentStrength: 意图强度
        """
        if not message_history:
            return IntentStrength.NONE
        
        recent_messages = self._get_recent_messages(message_history, limit=3, customer_only=True)
        all_content = " ".join([msg.get("content", "").lower() for msg in recent_messages])
        
        for strength, keywords in self.INTENT_STRENGTH_KEYWORDS.items():
            for keyword in keywords:
                if keyword in all_content:
                    return strength
        
        return IntentStrength.NONE
    
    def _score_to_strength(self, score: float) -> IntentStrength:
        """将分数转换为意图强度"""
        if score >= 30:
            return IntentStrength.VERY_STRONG
        elif score >= 20:
            return IntentStrength.STRONG
        elif score >= 10:
            return IntentStrength.MODERATE
        elif score > 0:
            return IntentStrength.WEAK
        else:
            return IntentStrength.NONE
    
    def _calculate_confidence(self, behavior_score: float, semantic_score: float,
                             interaction_score: float, emotion_score: float,
                             urgency_score: float, stage_score: float,
                             primary_intent: Optional[IntentDetail],
                             rejection_info: Optional[Dict]) -> float:
        """
        计算意图识别置信度
        
        Args:
            各维度分数和意图信息
            
        Returns:
            float: 置信度 (0-1)
        """
        confidence = 0.5
        
        if behavior_score > 0:
            confidence += 0.1
        if semantic_score > 0:
            confidence += 0.15
        if interaction_score > 0:
            confidence += 0.1
        if emotion_score != 0:
            confidence += 0.05
        if urgency_score > 0:
            confidence += 0.05
        if stage_score > 0:
            confidence += 0.05
        
        if primary_intent and primary_intent.confidence > 0.5:
            confidence += 0.1
        
        if rejection_info:
            confidence -= 0.1
        
        return min(max(confidence, 0.1), 1.0)
    
    def _predict_next_action(self, level: IntentLevel, primary_intent: Optional[IntentDetail],
                            purchase_stage: Optional[PurchaseStage],
                            urgency_level: Optional[UrgencyLevel],
                            conversion_signals: Optional[List[str]] = None) -> str:
        """
        预测客户下一步行为
        
        Args:
            level: 意向等级
            primary_intent: 主要意图
            purchase_stage: 购买阶段
            urgency_level: 紧迫程度
            
        Returns:
            str: 预测的下一步行为
        """
        conversion_signals = conversion_signals or []

        if "purchase_intent" in conversion_signals:
            return "推进预留并收集关键信息"
        if "availability_check" in conversion_signals or "stock_availability" in conversion_signals:
            return "优先确认可安排情况并补充必要说明"
        if "travel_date" in conversion_signals or "date_inquiry" in conversion_signals:
            return "先确认时间安排再推进后续说明"
        if "detail_inquiry" in conversion_signals:
            return "发送补充说明并确认关注点"
        if "price_quote" in conversion_signals:
            return "整理报价方案并确认关键需求"
        if purchase_stage == PurchaseStage.DECISION and urgency_level == UrgencyLevel.HIGH:
            return "推进预留安排并引导提供接收资料方式"
        if purchase_stage == PurchaseStage.INTENT and urgency_level in [UrgencyLevel.HIGH, UrgencyLevel.MEDIUM]:
            return "先确认可安排情况再推进资料发送"
        if level == IntentLevel.A:
            if urgency_level == UrgencyLevel.HIGH:
                return "高优先级跟进并推动留资"
            return "推进完整报价和详细说明"
        elif level == IntentLevel.B:
            if purchase_stage == PurchaseStage.DECISION:
                return "确认购买条件并准备预留"
            return "补齐需求信息并缩小选择范围"
        elif level == IntentLevel.C:
            if primary_intent and primary_intent.intent_type == PurchaseIntentType.COMPARISON:
                return "做方案对比并引导进一步咨询"
            return "继续摸底需求并建立兴趣"
        elif level == IntentLevel.D:
            return "轻跟进并等待下一轮需求"
        else:
            return "保持观察"
    
    def _recommend_response_type(self, level: IntentLevel, primary_intent: Optional[IntentDetail],
                                 rejection_info: Optional[Dict],
                                 intent_strength: Optional[IntentStrength],
                                 conversion_signals: Optional[List[str]] = None) -> str:
        """
        推荐回复类型
        
        Args:
            level: 意向等级
            primary_intent: 主要意图
            rejection_info: 拒绝信息
            intent_strength: 意图强度
            
        Returns:
            str: 推荐的回复类型
        """
        conversion_signals = conversion_signals or []

        if rejection_info:
            rejection_type = rejection_info.get("type", "")
            if rejection_type == "price":
                return "价格异议化解"
            elif rejection_type == "timing":
                return "延后跟进并保留意向"
            elif rejection_type == "competitor":
                return "突出方案差异化"
            else:
                return "顾虑化解"

        if "purchase_intent" in conversion_signals:
            return "预留推进"
        if "availability_check" in conversion_signals or "stock_availability" in conversion_signals:
            return "可安排情况确认"
        if "travel_date" in conversion_signals or "date_inquiry" in conversion_signals:
            return "时间安排确认"
        if "detail_inquiry" in conversion_signals:
            return "资料发送"
        if "price_quote" in conversion_signals:
            return "报价收口"
        
        if level == IntentLevel.A:
            if intent_strength == IntentStrength.VERY_STRONG:
                return "成交推进"
            return "高意向催单"
        elif level == IntentLevel.B:
            return "详细介绍"
        elif level == IntentLevel.C:
            return "需求培育"
        elif level == IntentLevel.D:
            return "轻度跟进"
        else:
            return "普通咨询"


_intent_analyzer = None


def get_intent_analyzer() -> IntentAnalyzer:
    """获取意向分析器"""
    global _intent_analyzer
    if _intent_analyzer is None:
        _intent_analyzer = IntentAnalyzer()
    return _intent_analyzer
