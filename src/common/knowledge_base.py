"""
智能客服知识库系统

基于GitHub开源项目最佳实践设计
支持知识管理、智能检索、拟人化回复、购买意愿分析
"""
import json
import uuid
import re
import random
import threading
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from loguru import logger

# 尝试导入jieba用于中文分词
try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False


from src.common.types.knowledge import KnowledgeCategory, KnowledgeItem as _UnifiedKnowledgeItem
from .knowledge_base_adapter import get_learning_knowledge_base_adapter


class PurchaseIntentLevel(Enum):
    """购买意愿等级"""
    VERY_HIGH = "A"    # 极高 - 立即跟进
    HIGH = "B"         # 高 - 优先跟进
    MEDIUM = "C"       # 中等 - 持续培育
    LOW = "D"          # 低 - 定期触达
    NONE = "E"         # 无 - 继续观察


class ReplyTone(Enum):
    """回复语气风格"""
    PROFESSIONAL = "professional"   # 专业正式
    FRIENDLY = "friendly"          # 亲切友好
    CASUAL = "casual"             # 轻松随意
    ENTHUSIASTIC = "enthusiastic"  # 热情积极


KnowledgeItem = _UnifiedKnowledgeItem


@dataclass
class PurchaseIntentResult:
    """购买意愿分析结果"""
    level: PurchaseIntentLevel
    score: float                          # 0-100分
    signals: List[str] = field(default_factory=list)   # 购买信号列表
    barriers: List[str] = field(default_factory=list)  # 购买障碍列表
    suggested_action: str = ""            # 建议行动
    confidence: float = 0.0               # 分析置信度


class KnowledgeBaseManager:
    """
    知识库管理器（已废弃，保留向后兼容）

    .. deprecated::
        此类为遗留实现，其核心功能已被 ``UnifiedKnowledgeService`` 完全覆盖。
        新代码请使用 ``src.common.knowledge_service_adapter.get_knowledge_service()``
        作为统一入口。

    负责知识的存储、检索、更新、删除等操作
    支持关键词匹配、语义相似度、多条件组合查询
    线程安全：所有写操作使用锁保护
    """
    
    def __init__(self, data_path: Path = None):
        from src.infrastructure.runtime_paths import get_knowledge_base_path

        self.data_path = data_path or get_knowledge_base_path()
        self.knowledge_items: List[KnowledgeItem] = []
        self._lock = threading.RLock()
        self._load_knowledge()
        if not self.knowledge_items:
            self._init_default_knowledge()
        self._quality_scorer = None

    def _get_quality_scorer(self):
        if self._quality_scorer is None:
            try:
                from src.common.knowledge_quality_scorer import KnowledgeQualityScorer
                self._quality_scorer = KnowledgeQualityScorer()
            except Exception:
                pass
        return self._quality_scorer

    def _load_knowledge(self):
        """从文件加载知识库"""
        from src.infrastructure.runtime_paths import get_data_dir, get_knowledge_base_path

        knowledge_paths = [
            get_knowledge_base_path(),
            get_data_dir() / "knowledge_unified.json",
            self.data_path,
        ]
        
        for path in knowledge_paths:
            try:
                if not path.exists():
                    continue
                if path.is_dir():
                    continue
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    items = [
                        KnowledgeItem.from_dict(item) for item in data
                    ]
                    if len(items) > len(self.knowledge_items):
                        self.knowledge_items = items
                        self.data_path = path
                        logger.info(f"从 {path} 加载 {len(self.knowledge_items)} 条知识")
            except Exception as e:
                logger.error(f"加载知识库 {path} 失败: {e}")
        
        if not self.knowledge_items:
            logger.warning("所有知识库加载失败，使用默认知识")
    
    def _save_knowledge(self):
        """保存知识库到文件（原子写入：先写临时文件再重命名，防止崩溃时数据损坏）"""
        try:
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            import tempfile
            temp_dir = str(self.data_path.parent)
            fd, temp_path = tempfile.mkstemp(suffix='.json', dir=temp_dir)
            try:
                with open(fd, 'w', encoding='utf-8') as f:
                    json.dump([item.to_dict() for item in self.knowledge_items],
                             f, ensure_ascii=False, indent=2)
                import os
                os.replace(temp_path, str(self.data_path))
            except Exception:
                import os
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                raise
            logger.info(f"已保存 {len(self.knowledge_items)} 条知识")
        except Exception as e:
            logger.error(f"保存知识库失败: {e}")
    
    def _init_default_knowledge(self):
        """
        初始化默认知识库
        
        包含常见客服场景的知识条目
        """
        if len(self.knowledge_items) > 0:
            return
        
        default_knowledge = [
            # 产品相关
            KnowledgeItem(
                question="产品介绍",
                answer="我们的智能获客系统是一款专业的营销自动化工具，支持多平台数据采集、智能客服、意向分析等功能。",
                category=KnowledgeCategory.PRODUCT.value,
                tags=["产品", "功能", "介绍"],
                keywords=["产品", "功能", "介绍", "是什么", "怎么样", "做什么的", "能做什么", "有什么用", "产品介绍", "系统介绍", "软件介绍", "产品说明"],
                aliases=["你们做什么的", "有什么产品", "产品有哪些", "你们是做什么的", "介绍一下产品", "产品是什么", "这是什么产品", "你们公司做什么", "主要业务是什么", "卖什么的", "产品怎么样", "产品好不好"],
                reply_templates=[
                    "您好！我们的智能获客系统是一款专业的营销自动化工具，主要功能包括多平台数据采集、智能客服、意向分析等。请问您想了解哪方面的详情呢？",
                    "感谢您的关注！我们的产品可以帮助企业实现自动化获客和智能客服，大大提升营销效率。您想了解具体哪个功能呢？",
                    "我们的系统主要解决企业获客难、客服成本高的问题，支持抖音、小红书等多平台。请问您主要想了解哪方面？"
                ],
                priority=10
            ),
            KnowledgeItem(
                question="产品功能详情",
                answer="主要功能包括：1.多平台数据采集 2.智能客服自动回复 3.客户意向分析 4.消息群发 5.数据统计分析",
                category=KnowledgeCategory.PRODUCT.value,
                tags=["功能", "详情", "特性"],
                keywords=["功能", "能做什么", "特性", "功能介绍", "功能详情", "核心功能", "主要功能", "有什么功能", "功能特点", "功能列表", "功能说明", "功能特性"],
                aliases=["有什么功能", "功能有哪些", "能干啥", "具体功能", "功能介绍下", "都有什么功能", "功能多吗", "功能全吗", "有什么特色功能", "核心功能是什么", "主要能做什么"],
                reply_templates=[
                    "我们的系统功能很丰富呢！主要包括：多平台数据采集、智能客服自动回复、客户意向分析、消息群发、数据统计等。您对哪个功能最感兴趣？",
                    "功能方面我来给您介绍一下~ 我们支持自动采集客户信息、智能回复消息、分析客户意向等级，还能批量发送消息。您想深入了解哪个？"
                ],
                priority=9
            ),
            # 价格相关
            KnowledgeItem(
                question="产品价格",
                answer="我们提供多种套餐：基础版2999元/月，专业版5999元/月，企业版9999元/月，支持定制化方案。",
                category=KnowledgeCategory.PRICE.value,
                tags=["价格", "费用", "收费"],
                keywords=["价格", "多少钱", "费用", "收费", "报价", "价位", "定价", "售价", "费用多少", "价格多少", "怎么收费", "收费标准", "收费模式", "价格表", "报价单"],
                aliases=["怎么收费", "贵不贵", "价格多少", "多少钱一个月", "费用怎么算", "价格怎么样", "收费方式", "怎么卖", "什么价位", "价格范围", "大概多少钱", "最低多少钱", "有价格表吗"],
                reply_templates=[
                    "关于价格，我们提供多种套餐供您选择：基础版2999元/月，专业版5999元/月，企业版9999元/月。根据您的需求，我可以帮您推荐最合适的方案~",
                    "价格方面您放心，我们有灵活的套餐方案。基础版2999起，也有企业定制方案。您大概有多少用户量呢？我帮您算算哪个更划算。",
                    "我们的价格很透明哦~ 基础版2999/月，专业版5999/月，企业版9999/月。现在还有优惠活动，您想了解吗？"
                ],
                priority=15
            ),
            KnowledgeItem(
                question="优惠活动",
                answer="当前优惠：年付享8折，新客户首月5折，推荐好友双方各得1个月免费使用。",
                category=KnowledgeCategory.PROMOTION.value,
                tags=["优惠", "折扣", "活动"],
                keywords=["优惠", "折扣", "活动", "便宜", "促销", "打折", "特价", "活动价", "优惠活动", "促销活动", "优惠码", "优惠券", "折扣码"],
                aliases=["有优惠吗", "能便宜吗", "打折吗", "有活动吗", "优惠多少", "能优惠吗", "有折扣吗", "怎么优惠", "促销活动", "现在有什么活动", "有特价吗", "能少点吗"],
                reply_templates=[
                    "正好我们最近有活动呢！年付享8折，新客户首月5折，推荐好友还有额外福利。您考虑哪种方案？",
                    "优惠肯定有的~ 现在年付8折，新客户首月半价。您是打算自己用还是公司用呢？我可以帮您算算最优惠的方案。",
                    "您来得正是时候！我们刚好有活动：年付8折，首月5折，还有推荐奖励。您想了解详情吗？"
                ],
                priority=14
            ),
            # 服务相关
            KnowledgeItem(
                question="售后服务",
                answer="我们提供7×24小时技术支持，1年内免费升级，终身维护，专属客户经理一对一服务。",
                category=KnowledgeCategory.AFTER_SALE.value,
                tags=["售后", "服务", "维护"],
                keywords=["售后", "服务", "维护", "技术支持", "售后服务", "客服", "服务保障", "服务承诺", "服务内容", "服务范围", "保修", "维护服务"],
                aliases=["售后怎么样", "有售后吗", "服务好吗", "售后服务怎么样", "服务怎么样", "有技术支持吗", "服务内容包括什么", "有保障吗", "出问题怎么办", "有问题找谁"],
                reply_templates=[
                    "售后服务您放心！我们提供7×24小时技术支持，1年内免费升级，还有专属客户经理一对一服务。有问题随时找我们~",
                    "服务方面我们很重视的~ 7×24小时在线支持，免费升级，终身维护。您完全不用担心后续问题！"
                ],
                priority=12
            ),
            KnowledgeItem(
                question="使用教程",
                answer="系统操作简单易上手，提供详细视频教程和文档，还有专人指导培训。",
                category=KnowledgeCategory.SERVICE.value,
                tags=["教程", "使用", "操作"],
                keywords=["教程", "怎么用", "操作", "使用方法", "使用教程", "操作教程", "怎么操作", "如何使用", "入门教程", "新手教程", "操作指南", "使用指南"],
                aliases=["怎么操作", "怎么使用", "难不难", "怎么上手", "容易学吗", "有教程吗", "有培训吗", "怎么学会", "操作复杂吗", "需要培训吗", "有文档吗"],
                reply_templates=[
                    "操作很简单的！我们有详细的视频教程和文档，还有专人指导培训。就算不懂技术也能快速上手~",
                    "您不用担心操作问题，系统设计得很人性化。我们有完整教程，还有客服手把手教您，很快就能学会！"
                ],
                priority=8
            ),
            # 合作相关
            KnowledgeItem(
                question="合作加盟",
                answer="我们欢迎各类合作伙伴，提供代理加盟、OEM定制、渠道合作等多种模式。",
                category=KnowledgeCategory.COOPERATION.value,
                tags=["合作", "加盟", "代理"],
                keywords=["合作", "加盟", "代理", "经销商", "分销", "渠道", "代理权", "加盟商", "合作伙伴", "OEM", "招商", "招募", "代理申请", "加盟申请"],
                aliases=["想代理", "可以加盟吗", "合作模式", "怎么代理", "加盟条件", "代理条件", "怎么合作", "有代理吗", "能加盟吗", "招商吗", "招募代理吗", "合作方式"],
                reply_templates=[
                    "太好了！我们非常欢迎合作伙伴~ 提供代理加盟、OEM定制、渠道合作等多种模式。您可以留下联系方式，我们的商务经理会详细跟您沟通。",
                    "感谢您对我们品牌的认可！合作模式很灵活，代理、OEM、渠道都可以。您方便留个微信或电话吗？我安排专人对接~"
                ],
                priority=16
            ),
            # 常见问题
            KnowledgeItem(
                question="如何开始使用",
                answer="注册账号后即可开始使用，支持免费试用7天，无需安装，网页端直接操作。",
                category=KnowledgeCategory.FAQ.value,
                tags=["开始", "试用", "注册"],
                keywords=["怎么开始", "如何使用", "试用", "注册", "开始使用", "免费试用", "试用申请", "开通账号", "注册账号", "怎么注册", "如何注册"],
                aliases=["怎么注册", "能试用吗", "免费吗", "怎么开通", "怎么申请", "能免费试用吗", "试用多久", "怎么开始用", "如何开始", "注册流程"],
                reply_templates=[
                    "开始使用很简单！注册账号就能试用7天，不用安装软件，网页直接操作。我现在就可以帮您开通试用账号~",
                    "您可以先免费试用7天体验一下功能。注册很简单，几分钟就搞定。需要我帮您开通吗？"
                ],
                priority=11
            ),
            KnowledgeItem(
                question="数据安全",
                answer="数据采用银行级加密存储，支持私有化部署，签署保密协议，确保数据安全。",
                category=KnowledgeCategory.POLICY.value,
                tags=["安全", "隐私", "数据"],
                keywords=["安全", "隐私", "数据", "保密", "加密", "数据安全", "隐私保护", "信息安全", "数据保护", "安全吗", "保密协议", "数据泄露"],
                aliases=["数据安全吗", "会泄露吗", "隐私保护", "安全吗", "数据怎么保护", "有加密吗", "安全措施", "隐私安全吗", "数据会不会丢", "数据存在哪"],
                reply_templates=[
                    "数据安全您绝对放心！我们采用银行级加密技术，支持私有化部署，还会签署保密协议。您的数据只属于您~",
                    "安全是我们的首要考虑！银行级加密、私有化部署、保密协议三重保障。很多大企业都在用我们的系统呢~"
                ],
                priority=13
            ),
            KnowledgeItem(
                question="支持平台",
                answer="目前支持抖音、小红书、快手、微博等主流平台，持续扩展中。",
                category=KnowledgeCategory.PRODUCT.value,
                tags=["平台", "支持", "渠道"],
                keywords=["平台", "支持", "抖音", "小红书", "支持平台", "支持哪些", "平台支持", "能用什么平台", "兼容平台", "支持渠道"],
                aliases=["支持哪些平台", "能用什么平台", "有哪些平台", "支持抖音吗", "支持小红书吗", "支持快手吗", "能接哪些平台", "平台多吗", "支持多少平台"],
                reply_templates=[
                    "目前支持抖音、小红书、快手、微博等主流平台，还在持续扩展中。您主要做哪个平台呢？",
                    "主流平台都支持哦~ 抖音、小红书、快手、微博都可以。您想了解哪个平台的详细功能？"
                ],
                priority=7
            ),
            KnowledgeItem(
                question="付款方式",
                answer="支持多种付款方式：微信支付、支付宝、银行转账、对公转账等，可开具正规发票。",
                category=KnowledgeCategory.FAQ.value,
                tags=["付款", "支付", "发票"],
                keywords=["付款", "支付", "付款方式", "支付方式", "怎么付款", "怎么支付", "发票", "开票", "转账", "对公转账"],
                aliases=["怎么付款", "能开发票吗", "怎么支付", "付款方式有哪些", "能对公转账吗", "有发票吗", "发票怎么开", "支持什么支付方式"],
                reply_templates=[
                    "我们支持多种付款方式：微信、支付宝、银行转账都可以。对公转账也没问题，还可以开具正规发票。您想用哪种方式呢？",
                    "付款很方便的~ 微信、支付宝、银行卡都支持。需要发票的话我们也可以开具。您有什么偏好的付款方式吗？"
                ],
                priority=10
            ),
            KnowledgeItem(
                question="版本升级",
                answer="系统定期免费升级，老用户可享受升级优惠，新功能自动推送。",
                category=KnowledgeCategory.FAQ.value,
                tags=["升级", "更新", "版本"],
                keywords=["升级", "更新", "版本", "升级费用", "免费升级", "版本更新", "新版本", "功能更新", "升级政策"],
                aliases=["能升级吗", "升级收费吗", "有新版本吗", "怎么升级", "升级要钱吗", "版本怎么更新", "有更新吗"],
                reply_templates=[
                    "系统升级是免费的哦！我们会定期更新功能，您不需要额外付费。新功能上线会自动推送，很方便的~",
                    "升级方面您放心，老用户可以享受免费升级。我们持续在优化产品，新功能会自动更新。您还有其他问题吗？"
                ],
                priority=8
            ),
            KnowledgeItem(
                question="账号问题",
                answer="账号支持手机号、邮箱注册，可找回密码，支持多设备登录。",
                category=KnowledgeCategory.FAQ.value,
                tags=["账号", "登录", "密码"],
                keywords=["账号", "登录", "密码", "注册", "账号问题", "登录问题", "密码找回", "账号安全", "忘记密码"],
                aliases=["忘记密码怎么办", "登录不了", "账号被封了", "怎么找回密码", "账号有问题", "登录不上", "密码忘了"],
                reply_templates=[
                    "账号问题我可以帮您解决~ 如果忘记密码可以通过手机号或邮箱找回。登录不了的话，您可以告诉我具体情况，我来帮您排查。",
                    "别担心，账号问题很好解决的。您是忘记密码还是登录遇到问题？我可以一步步指导您操作。"
                ],
                priority=9
            ),
        ]
        
        self.knowledge_items = default_knowledge
        self._save_knowledge()
        logger.info(f"已初始化 {len(default_knowledge)} 条默认知识")
    
    def add_knowledge(self, item: KnowledgeItem) -> bool:
        """添加知识条目（线程安全）"""
        with self._lock:
            try:
                scorer = self._get_quality_scorer()
                if scorer is not None:
                    try:
                        quality_result = scorer.evaluate_quality(item.question, item.answer)
                        if quality_result and hasattr(quality_result, 'total_score') and quality_result.total_score < 0.3:
                            logger.warning(f"知识条目质量过低被拒绝: {item.id} score={quality_result.total_score:.2f}")
                            return False
                    except Exception:
                        pass
                self.knowledge_items.append(item)
                self._save_knowledge()
                logger.info(f"添加知识: {item.question}")
                return True
            except Exception as e:
                logger.error(f"添加知识失败: {e}")
                return False
    
    def update_knowledge(self, item_id: str, updates: Dict) -> bool:
        """更新知识条目（线程安全）"""
        with self._lock:
            for i, item in enumerate(self.knowledge_items):
                if item.id == item_id:
                    for key, value in updates.items():
                        if hasattr(item, key):
                            setattr(item, key, value)
                    item.updated_at = datetime.now().isoformat()
                    self._save_knowledge()
                    logger.info(f"更新知识: {item.question}")
                    return True
            return False
    
    def delete_knowledge(self, item_id: str) -> bool:
        """删除知识条目（线程安全）"""
        with self._lock:
            for i, item in enumerate(self.knowledge_items):
                if item.id == item_id:
                    self.knowledge_items.pop(i)
                    self._save_knowledge()
                    logger.info(f"删除知识: {item.question}")
                    return True
        return False
    
    def get_knowledge(self, item_id: str) -> Optional[KnowledgeItem]:
        """获取单条知识"""
        for item in self.knowledge_items:
            if item.id == item_id:
                return item
        return None
    
    def search(self, query: str, top_k: int = 5,
               category: str = None,
               use_fuzzy: bool = True) -> List[Tuple[KnowledgeItem, float]]:
        """
        搜索知识（带顶层异常保护，防止检索异常导致消息处理流程崩溃）

        .. deprecated::
            此方法已被 ``UnifiedKnowledgeService.search()`` 取代。
            请通过 ``get_knowledge_service().search(...)`` 调用。

        基于业界最佳实践优化:
        - 多维度匹配 (关键词、别名、标签、内容)
        - 模糊匹配和同义词扩展
        - 语义相似度计算
        - 业务关键词加权
        - 优先级排序
        
        Args:
            query: 搜索关键词
            top_k: 返回数量
            category: 分类过滤
            use_fuzzy: 是否使用模糊匹配
            
        Returns:
            List[Tuple[KnowledgeItem, float]]: 知识条目和匹配分数
        """
        try:
            return self._search_impl(query, top_k, category, use_fuzzy)
        except Exception as e:
            logger.error(f"知识库搜索异常，返回空结果: query={query[:50]}, error={e}")
            return []

    def _search_impl(self, query: str, top_k: int = 5,
                     category: str = None,
                     use_fuzzy: bool = True) -> List[Tuple[KnowledgeItem, float]]:
        """搜索知识内部实现"""
        results = []
        query_lower = query.lower()
        
        def _tokenize_text(text: str) -> set:
            """将文本分词为词语集合（使用jieba分词，避免字符级拆分）"""
            if JIEBA_AVAILABLE:
                try:
                    tokens = set(jieba.cut(text))
                    return {w for w in tokens if len(w.strip()) >= 2}
                except Exception as e:
                    logger.debug(f"jieba分词失败: {e}")
            words = set()
            for i in range(len(text) - 1):
                words.add(text[i:i+2])
            return words
        
        query_words = _tokenize_text(query_lower)
        
        business_keyword_weights = {
            '获客': 2.5, '客户': 1.8, '添加': 2.0, '转化': 2.0,
            'B2B': 2.5, '企业': 1.8, '营销': 1.5, '推广': 1.5,
            '线索': 1.8, '商机': 1.8, '引流': 1.6, '拉新': 1.6,
            '价格': 2.0, '费用': 2.0, '功能': 1.6, '效果': 1.6,
            '怎么': 1.3, '如何': 1.3, '什么': 1.2,
            '活动': 1.6, '策划': 1.6, '方案': 1.4,
            '教育': 3.0, '培训': 2.5, '电商': 2.5, '金融': 2.5,
            '医疗': 2.5, '美妆': 2.5, '本地': 2.0, '行业': 1.8,
            '产品': 1.8, '介绍': 1.5, '多少钱': 2.2, '收费': 2.0,
            '优惠': 2.0, '折扣': 1.8, '售后': 2.0, '服务': 1.6,
            '合作': 2.0, '加盟': 2.2, '代理': 2.2, '试用': 1.8,
            '演示': 1.8, '安全': 1.8, '数据': 1.6, '升级': 1.5,
            '账号': 1.5, '登录': 1.4, '注册': 1.4, '付款': 1.8,
            '发票': 1.6, '物流': 1.5, '发货': 1.5, '退款': 1.8,
            '投诉': 2.5, '问题': 1.4, '帮助': 1.3, '联系': 1.5,
        }
        
        combo_keywords = {
            ('教育', '获客'): 20.0,
            ('培训', '获客'): 18.0,
            ('电商', '获客'): 16.0,
            ('金融', '获客'): 16.0,
            ('B2B', '获客'): 18.0,
            ('企业', '获客'): 16.0,
            ('医疗', '获客'): 15.0,
            ('美妆', '获客'): 14.0,
            ('本地', '获客'): 12.0,
            ('价格', '优惠'): 12.0,
            ('产品', '功能'): 10.0,
            ('怎么', '使用'): 8.0,
            ('如何', '操作'): 8.0,
        }
        
        synonym_groups = {
            '价格': ['多少钱', '费用', '收费', '价位', '报价', '定价'],
            '产品': ['商品', '系统', '软件', '平台', '工具'],
            '功能': ['特性', '能力', '特点', '作用'],
            '购买': ['买', '订购', '下单', '采购', '订阅'],
            '优惠': ['折扣', '活动', '特价', '促销', '便宜'],
            '售后': ['客服', '服务', '支持', '维护'],
            '怎么': ['如何', '怎样', '怎么操作', '怎么使用'],
            '好': ['不错', '可以', '行', '棒', '优秀'],
        }
        
        category_hint_keywords = {
            'price': {'价格', '多少钱', '费用', '收费', '价位', '报价', '定价', '优惠', '折扣', '便宜', '月付', '年付', '套餐', '付款', '发票', '退款'},
            'product': {'产品', '商品', '系统', '软件', '平台', '工具', '功能', '特性', '能力', '特点', '介绍', '优势'},
            'service': {'服务', '售后', '客服', '支持', '维护', '培训', '技术支持'},
            'company': {'公司', '企业', '团队', '员工', '规模', '成立', '资质'},
            'faq': {'怎么', '如何', '怎样', '什么', '为什么', '是否', '可以', '能'},
            'after_sale': {'售后', '退款', '退货', '投诉', '问题', '故障', '维修'},
            'cooperation': {'合作', '加盟', '代理', '伙伴', '渠道', '对接'},
        }
        
        query_tokens = set()
        
        if JIEBA_AVAILABLE:
            try:
                query_tokens = set(jieba.cut(query_lower))
                query_tokens = {w for w in query_tokens if len(w) >= 2 and w not in ['怎么', '如何', '什么', '可以', '能', '吗', '的', '了', '是', '在', '有', '和', '与', '或']}
            except Exception:
                for i in range(len(query_lower) - 1):
                    bigram = query_lower[i:i+2]
                    if len(bigram) >= 2:
                        query_tokens.add(bigram)
        else:
            for i in range(len(query_lower) - 1):
                bigram = query_lower[i:i+2]
                if len(bigram) >= 2:
                    query_tokens.add(bigram)
        
        expanded_tokens = set(query_tokens)
        for token in query_tokens:
            for key, synonyms in synonym_groups.items():
                if token in synonyms or token == key:
                    expanded_tokens.add(key)
                    expanded_tokens.update(synonyms)
        
        fuzzy_matcher = None
        if use_fuzzy:
            try:
                from src.common.fuzzy_matcher import get_fuzzy_matcher
                fuzzy_matcher = get_fuzzy_matcher()
            except Exception as e:
                logger.debug(f"模糊匹配器加载失败: {e}")
        
        query_keywords = set()
        if fuzzy_matcher:
            try:
                query_keywords = fuzzy_matcher.extract_keywords(query)
            except Exception as e:
                logger.debug(f"模糊匹配器提取关键词失败: {e}")
        
        for item in self.knowledge_items:
            if not item.enabled:
                continue
            if category and item.category != category:
                continue
            
            score = 0.0
            
            for keyword in item.keywords:
                kw_lower = keyword.lower()
                if kw_lower in query_tokens:
                    base_weight = business_keyword_weights.get(kw_lower, 1.0)
                    score += 30.0 * base_weight
                elif kw_lower in expanded_tokens:
                    base_weight = business_keyword_weights.get(kw_lower, 1.0)
                    score += 20.0 * base_weight
                elif kw_lower in query_lower:
                    base_weight = business_keyword_weights.get(kw_lower, 1.0)
                    score += 15.0 * base_weight
                elif query_lower in kw_lower:
                    score += 10.0
            
            if query_lower == item.question.lower():
                score += 120.0
            elif query_lower in item.question.lower():
                score += 80.0
            else:
                question_lower = item.question.lower()
                # 检查问题文本中是否包含查询中的关键短语
                key_phrases = ['多少钱', '价格', '费用', '收费', '优惠', '折扣', 
                               '功能', '平台', '售后', '合作', '公司', '产品',
                               '怎么', '如何', '什么', '为什么', '是否']
                # 不同短语有不同的意图强度
                phrase_intent_weights = {
                    '多少钱': 3.0, '价格': 3.0, '费用': 2.5, '收费': 2.5,
                    '优惠': 2.5, '折扣': 2.5, '售后': 2.5, '合作': 2.5,
                    '功能': 2.0, '平台': 2.0, '怎么': 1.5, '如何': 1.5,
                    '什么': 1.2, '为什么': 1.2, '是否': 1.2,
                    '公司': 1.0, '产品': 1.0,
                }
                for phrase in key_phrases:
                    if phrase in query_lower and phrase in question_lower:
                        weight = phrase_intent_weights.get(phrase, 1.0)
                        score += 35.0 * weight
                if JIEBA_AVAILABLE:
                    try:
                        question_tokens = set(jieba.cut(question_lower))
                        common_tokens = query_tokens & question_tokens
                        if common_tokens:
                            token_overlap = len(common_tokens) / max(len(query_tokens), 1)
                            score += 25.0 * token_overlap * len(common_tokens)
                        
                        expanded_common = expanded_tokens & question_tokens
                        if expanded_common:
                            score += 10.0 * len(expanded_common)
                    except Exception as e:
                        logger.debug(f"扩展词匹配失败: {e}")
            
            for alias in item.aliases:
                alias_lower = alias.lower()
                if alias_lower == query_lower:
                    score += 60.0
                elif alias_lower in query_lower or query_lower in alias_lower:
                    score += 35.0
                else:
                    alias_tokens = _tokenize_text(alias_lower)
                    common = query_tokens & alias_tokens
                    if common:
                        score += 20.0 * len(common)
                    
                    expanded_common = expanded_tokens & alias_tokens
                    if expanded_common:
                        score += 10.0 * len(expanded_common)
            
            item_tags = getattr(item, 'tags', []) or []
            for tag in item_tags:
                tag_lower = tag.lower()
                if tag_lower in query_lower:
                    score += 18.0
                elif tag_lower in expanded_tokens:
                    score += 10.0
            
            if query_lower in item.answer.lower():
                score += 12.0
            else:
                answer_words = _tokenize_text(item.answer.lower())
                common = query_words & answer_words
                if common:
                    score += 4.0 * len(common)
                
                expanded_common = expanded_tokens & answer_words
                if expanded_common:
                    score += 2.0 * len(expanded_common)
            
            answer_text = item.answer.lower()
            for kw, weight in business_keyword_weights.items():
                if kw in query_lower and kw in answer_text:
                    score += 12.0 * weight
                elif kw in expanded_tokens and kw in answer_text:
                    score += 6.0 * weight
            
            for (kw1, kw2), bonus in combo_keywords.items():
                if kw1 in query_lower and kw2 in query_lower:
                    if kw1 in answer_text or kw1 in item.question.lower():
                        if kw2 in answer_text or kw2 in item.question.lower():
                            score += bonus
                elif (kw1 in query_lower and kw2 in expanded_tokens) or (kw1 in expanded_tokens and kw2 in query_lower):
                    if kw1 in answer_text or kw2 in answer_text:
                        score += bonus * 0.5
            
            if fuzzy_matcher and query_keywords:
                try:
                    match_result = fuzzy_matcher.calculate_match_score(
                        query, item.question,
                        query_keywords=query_keywords
                    )
                    if match_result.score > 0:
                        score += match_result.score * 0.6
                    
                    item_keywords = fuzzy_matcher.extract_keywords(item.question)
                    expanded_query = fuzzy_matcher.expand_with_synonyms(query_keywords)
                    expanded_item = fuzzy_matcher.expand_with_synonyms(item_keywords)
                    
                    synonym_match = expanded_query & expanded_item
                    if synonym_match:
                        score += len(synonym_match) * 8.0
                    
                    if hasattr(fuzzy_matcher, 'calculate_similarity'):
                        try:
                            similarity = fuzzy_matcher.calculate_similarity(query, item.question)
                            if similarity > 0.7:
                                score += 40.0 * similarity
                            elif similarity > 0.5:
                                score += 20.0 * similarity
                        except Exception as e:
                            logger.debug(f"模糊匹配评分失败: {e}")
                except Exception as e:
                    logger.debug(f"模糊匹配失败: {e}")
            
            score += item.priority * 1.0
            
            # 分类匹配加分：当查询暗示了特定分类时，该分类条目获得额外加分
            # 意图性强的关键词（如"多少钱"、"售后"）权重更高
            category_hint_weights = {
                'price': 1.5,
                'after_sale': 1.2,
                'cooperation': 1.2,
                'service': 1.0,
                'company': 0.8,
                'product': 0.5,
                'faq': 0.4,
            }
            for hint_category, hint_keywords in category_hint_keywords.items():
                hint_match_count = sum(1 for kw in hint_keywords if kw in query_lower)
                if hint_match_count > 0 and item.category == hint_category:
                    weight = category_hint_weights.get(hint_category, 1.0)
                    score += 15.0 * hint_match_count * weight
            
            if item.use_count > 0:
                usage_bonus = min(item.use_count * 0.5, 30.0)
                score += usage_bonus
            
            # 效果评分加分（基于学习引擎计算）
            try:
                from .intelligent_learning_engine import get_learning_engine
                engine = get_learning_engine()
                effectiveness_boost = engine.get_search_boost(item.id)
                score += effectiveness_boost
            except Exception as e:
                logger.debug(f"学习引擎加权失败: {e}")
            positive_feedback = getattr(item, 'positive_feedback_count', 0) or 0
            negative_feedback = getattr(item, 'negative_feedback_count', 0) or 0
            if positive_feedback > 0:
                score += min(positive_feedback * 3.0, 25.0)
            if negative_feedback > 0:
                score -= min(negative_feedback * 5.0, 30.0)
            
            recency_bonus = 0.0
            if hasattr(item, 'last_used_at') and item.last_used_at:
                try:
                    from datetime import datetime, timedelta
                    last_used = datetime.fromisoformat(item.last_used_at)
                    days_ago = (datetime.now() - last_used).days
                    if days_ago <= 7:
                        recency_bonus = max(0, 10.0 - days_ago * 1.0)
                        score += recency_bonus
                except Exception as e:
                    logger.debug(f"时效性加权失败: {e}")
            
            if score > 0:
                results.append((item, score))
        
        results.sort(key=lambda x: x[1], reverse=True)

        if results:
            scorer = self._get_quality_scorer()
            if scorer is not None:
                adjusted = []
                for item, score in results:
                    try:
                        quality_result = scorer.evaluate_quality(item.question, item.answer)
                        quality_factor = getattr(quality_result, 'total_score', 0.5) if quality_result else 0.5
                        adjusted.append((item, score * max(quality_factor, 0.3)))
                    except Exception:
                        adjusted.append((item, score))
                results = adjusted
                results.sort(key=lambda x: x[1], reverse=True)

        if results and len(results) > 1:
            best_score = results[0][1]
            if best_score > 50:
                filtered_results = []
                for item, score in results:
                    if score >= best_score * 0.3:
                        filtered_results.append((item, score))
                results = filtered_results[:top_k]
        
        return results[:top_k]

    def search_semantic(self, query: str, top_k: int = 5,
                        category: str = None,
                        vector_retriever=None) -> List[Tuple['KnowledgeItem', float]]:
        """
        简化语义搜索（分层评分架构）

        .. deprecated::
            此方法已被 ``UnifiedKnowledgeService.search(use_vector=True)`` 取代。
            请通过 ``get_knowledge_service().search(..., use_vector=True)`` 调用。

        第一层：别名/问题精确匹配（高权重）
        第二层：关键词匹配（中权重）
        第三层：业务规则微调（低权重）

        相比search()方法，此方法大幅简化评分逻辑，降低维护成本

        Args:
            query: 搜索关键词
            top_k: 返回数量
            category: 分类过滤
            vector_retriever: 向量检索器（可选，用于语义匹配）

        Returns:
            List[Tuple[KnowledgeItem, float]]: 知识条目和匹配分数
        """
        results = []
        query_lower = query.lower()

        for item in self.knowledge_items:
            if not item.enabled:
                continue
            if category and item.category != category:
                continue

            score = 0.0

            # 第一层：精确匹配（高权重）
            if query_lower == item.question.lower():
                score += 100.0
            elif query_lower in item.question.lower():
                score += 60.0

            for alias in (item.aliases or []):
                alias_lower = alias.lower()
                if alias_lower == query_lower:
                    score += 80.0
                elif alias_lower in query_lower or query_lower in alias_lower:
                    score += 40.0

            # 第二层：关键词匹配（中权重）
            for keyword in (item.keywords or []):
                if keyword.lower() in query_lower:
                    score += 25.0

            for tag in (item.tags or []):
                if tag.lower() in query_lower:
                    score += 15.0

            # 第三层：业务规则微调（低权重）
            score += (item.priority or 0) * 0.5
            if item.use_count > 0:
                score += min(item.use_count * 0.3, 15.0)

            if score > 0:
                results.append((item, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def get_by_category(self, category: str) -> List[KnowledgeItem]:
        """按分类获取知识"""
        return [item for item in self.knowledge_items 
                if item.category == category and item.enabled]

    def list_knowledge_items(self) -> List[KnowledgeItem]:
        """统一知识读取入口，兼容新旧调用方。"""
        return list(self.knowledge_items)

    def get_knowledge_list(self) -> List[KnowledgeItem]:
        """兼容旧适配层和学习链的知识列表读取方法。"""
        return self.list_knowledge_items()

    def get_all_items(self) -> List[KnowledgeItem]:
        """兼容统一读取边界所需的只读列表接口。"""
        return self.list_knowledge_items()
    
    def get_all_categories(self) -> List[str]:
        """获取所有分类"""
        return list(set(item.category for item in self.knowledge_items))
    
    def record_usage(self, item_id: str):
        """记录知识使用"""
        with self._lock:
            for item in self.knowledge_items:
                if item.id == item_id:
                    item.use_count += 1
                    item.last_used_at = datetime.now().isoformat()
                    self._pending_save = True
                    break

    def flush(self):
        """强制保存知识库（如果有待保存的更改）"""
        if getattr(self, '_pending_save', False):
            self._save_knowledge()
            self._pending_save = False


class PurchaseIntentAnalyzer:
    """
    购买意愿智能分析引擎
    
    基于多维度信号分析客户的购买意愿
    包括：关键词信号、行为信号、上下文信号
    """
    
    # 购买信号关键词 (正面)
    PURCHASE_SIGNALS = {
        "strong_intent": {
            "keywords": [
                "想买", "要买", "购买", "下单", "付款", "成交", "签约", "定下来",
                "准备买", "打算买", "需要买", "马上买", "立即购买", "确定要",
                "就这个了", "决定买", "一定要", "非买不可", "今天买",
                "现在就要", "急用", "急需", "马上要", "尽快买",
                "订购", "预订", "预定", "预购", "抢购", "团购", "拼单",
                "下单子", "开单", "成交吧", "就这样吧", "可以签了",
                "没问题了", "不用再看了", "就选这个", "确定了"
            ],
            "weight": 30
        },
        "price_inquiry": {
            "keywords": [
                "价格", "多少钱", "费用", "报价", "怎么收费",
                "收费模式", "收费标准", "价位", "成本", "预算",
                "定价", "售价", "单价", "总价", "付款方式",
                "分期", "首付", "能便宜吗", "有优惠吗", "最低价",
                "批发价", "团购价", "会员价", "活动价", "多少钱一个",
                "价格多少", "价格怎么样", "价格合适吗", "价格能不能谈",
                "给个价", "报个价", "最终价格", "成交价", "底价",
                "市场价", "指导价", "建议零售价", "实际价格", "到手价",
                "性价比", "划算", "值不值", "贵不贵", "价格合理",
                "价格表", "报价单", "费用明细", "收费明细"
            ],
            "weight": 20
        },
        "comparison": {
            "keywords": [
                "对比", "比较", "区别", "哪个好", "优缺点",
                "差异", "不一样", "有什么不同", "哪个更好", "哪个合适",
                "怎么选", "选择哪个", "对比一下", "比较一下",
                "和...比", "相比", "对比其他", "竞品对比", "横向对比",
                "性价比", "划算", "值得买", "推荐哪个",
                "纵向对比", "功能对比", "价格对比", "性能对比",
                "优势对比", "劣势对比", "参数对比", "配置对比",
                "同类产品", "竞品分析", "替代方案", "备选方案",
                "哪个更推荐", "哪个更适合", "帮我选一下"
            ],
            "weight": 15
        },
        "timeline": {
            "keywords": [
                "什么时候", "多久", "几天", "什么时候能",
                "需要多长时间", "多长时间", "什么时候可以",
                "什么时候发货", "什么时候到", "几天到", "多久能到",
                "什么时候能用", "什么时候开始", "什么时候完成",
                "工期", "周期", "时间", "日期", "截止时间",
                "交期", "交货期", "交付时间", "到货时间", "发货时间",
                "处理时间", "响应时间", "上线时间", "部署时间",
                "实施周期", "开发周期", "测试周期", "验收时间",
                "合同期限", "服务期限", "有效期", "截止日期",
                "最晚时间", "最早时间", "预计时间", "大概时间",
                "能加急吗", "能快点吗", "能提前吗"
            ],
            "weight": 10
        },
        "contact_request": {
            "keywords": [
                "联系", "电话", "微信", "加微信", "留电话",
                "联系方式", "手机号", "微信号", "QQ", "邮箱",
                "地址", "加好友", "加你", "私聊", "私信",
                "留个联系方式", "怎么联系", "能留个电话吗",
                "能加微信吗", "方便联系吗", "回电话", "电话沟通",
                "语音沟通", "视频沟通", "面谈", "见面聊",
                "上门拜访", "约时间", "约个时间", "方便的时候",
                "工作时间", "非工作时间", "紧急联系", "商务联系",
                "技术联系", "售后联系", "客服电话", "服务热线",
                "400电话", "官方微信", "官方QQ", "客服邮箱",
                "留个微信", "留个电话", "怎么加你", "怎么找到你们"
            ],
            "weight": 25
        },
        "demo_request": {
            "keywords": [
                "演示", "试用", "体验", "看看效果",
                "试一下", "试用一下", "免费试用", "试用版",
                "体验版", "demo", "演示一下", "看下效果",
                "先试试", "测试一下", "测试版", "样机",
                "样品", "试用账号", "试用权限", "开通试用",
                "申请试用", "先体验", "想试试",
                "试用申请", "试用期限", "试用功能", "试用范围",
                "免费体验", "体验账号", "体验中心", "在线演示",
                "预约演示", "上门演示", "远程演示", "视频演示",
                "功能演示", "效果演示", "案例演示", "实际操作",
                "先体验再买", "先试用再决定", "试用满意再付款"
            ],
            "weight": 20
        },
        "quantity_inquiry": {
            "keywords": [
                "多少个", "几个", "数量", "批量", "大量",
                "多少量", "起订量", "最小起订", "批量购买",
                "批发数量", "库存", "有货吗", "有现货吗",
                "够不够", "有多少", "能供应多少", "产能",
                "库存多少", "现货数量", "备货", "备货量", "库存情况",
                "缺货", "断货", "补货时间", "预售", "限量",
                "限购", "最大购买量", "最小购买量", "购买限制",
                "供应能力", "日产量", "月产量", "年产量",
                "供货周期", "供货能力", "稳定供货", "长期供货",
                "大批量", "小批量", "定制数量"
            ],
            "weight": 15
        },
        "cooperation_intent": {
            "keywords": [
                "合作", "加盟", "代理", "代理权", "经销",
                "分销", "渠道", "加盟商", "合作伙伴", "OEM",
                "贴牌", "定制", "代工", "招商", "招募",
                "签约", "合同", "协议", "授权", "独家",
                "区域代理", "总代理", "一级代理", "市级代理",
                "省级代理", "合伙人", "联营", "战略合作",
                "商务合作", "项目合作", "渠道合作", "代理申请",
                "加盟申请", "代理条件", "加盟条件", "代理费用", "加盟费",
                "保证金", "返点", "提成", "佣金", "分成", "代理政策",
                "加盟政策", "扶持", "培训支持", "市场支持", "技术支持"
            ],
            "weight": 25
        },
        "feature_inquiry": {
            "keywords": [
                "功能", "特点", "优势", "能做什么", "有什么用",
                "功能介绍", "功能详情", "核心功能", "主要功能",
                "支持什么", "能实现什么", "有什么能力", "功能列表",
                "特性", "功能演示", "功能说明", "功能详解", "功能对比",
                "功能需求", "定制功能", "扩展功能", "插件功能",
                "API接口", "二次开发", "集成能力", "兼容性",
                "有什么特色", "有什么亮点", "有什么卖点",
                "能解决什么问题", "有什么好处", "有什么价值"
            ],
            "weight": 12
        },
        "service_inquiry": {
            "keywords": [
                "售后", "服务", "维护", "技术支持", "客服",
                "培训", "指导", "教程", "文档", "帮助",
                "售后怎么样", "有售后吗", "服务好吗", "保修",
                "服务内容", "服务范围", "服务条款", "服务协议", "服务承诺",
                "服务标准", "服务质量", "服务响应", "服务时效",
                "上门服务", "远程服务", "现场服务", "驻场服务",
                "培训服务", "实施服务", "运维服务", "升级服务",
                "维护服务", "保修服务", "延保服务", "VIP服务",
                "专属服务", "一对一服务", "7x24服务", "节假日服务"
            ],
            "weight": 10
        },
        "security_inquiry": {
            "keywords": [
                "安全", "隐私", "数据", "保密", "加密",
                "数据安全吗", "会泄露吗", "隐私保护", "信息安全",
                "数据安全", "数据保护", "传输加密", "存储加密",
                "数据备份", "容灾", "灾备", "数据恢复", "数据迁移",
                "权限管理", "访问控制", "身份认证", "安全认证",
                "合规", "资质", "ISO认证", "等保", "安全等级",
                "数据泄露", "隐私政策", "保密协议", "NDA"
            ],
            "weight": 8
        },
        "payment_method": {
            "keywords": [
                "怎么付款", "支付方式", "付款方式", "怎么付",
                "刷卡", "转账", "支付宝", "微信支付", "对公转账",
                "开发票", "发票", "收据", "账期",
                "在线支付", "银行转账", "银联支付", "信用卡支付",
                "分期付款", "分期支付", "尾款", "全款",
                "定金", "预付款", "货到付款", "月结",
                "增值税发票", "普通发票", "专用发票",
                "发票抬头", "税号", "付款凭证", "支付凭证"
            ],
            "weight": 18
        },
        "delivery_inquiry": {
            "keywords": [
                "发货", "配送", "快递", "物流", "送货",
                "发货方式", "配送方式", "快递公司", "物流公司",
                "运费", "包邮", "到付", "自提", "送货上门",
                "指定地点", "代收货款", "物流查询", "快递查询",
                "物流跟踪", "发货通知", "签收", "验货",
                "收货确认", "配送范围", "配送时间",
                "什么时候发货", "什么时候到货", "几天能到"
            ],
            "weight": 12
        },
        "contract_inquiry": {
            "keywords": [
                "合同", "协议", "签约", "合同条款", "协议内容",
                "服务协议", "购买合同", "销售合同", "代理合同",
                "合作协议", "保密协议", "框架协议", "补充协议",
                "合同期限", "合同续签", "合同变更", "合同解除",
                "违约责任", "争议解决", "法律效力", "盖章", "签字",
                "能签合同吗", "需要签合同吗", "合同怎么签"
            ],
            "weight": 15
        },
        "upgrade_renewal": {
            "keywords": [
                "升级", "更新", "续费", "续期", "到期",
                "续订", "延长", "续费优惠", "版本升级",
                "功能更新", "系统升级", "新版本", "最新版",
                "免费升级", "付费升级", "升级费用", "升级政策",
                "版本对比", "版本历史", "更新日志", "发布说明",
                "新功能", "功能优化", "老用户优惠", "续费折扣"
            ],
            "weight": 15
        },
        "account_inquiry": {
            "keywords": [
                "账号", "账户", "登录", "注册", "账号问题",
                "登录问题", "注册问题", "密码", "密码找回", "密码重置",
                "账号注销", "账号冻结", "账号解冻", "账号安全",
                "账号绑定", "账号解绑", "手机绑定", "邮箱绑定",
                "实名认证", "企业认证", "资质认证", "认证问题",
                "怎么注册", "怎么登录", "忘记密码"
            ],
            "weight": 8
        },
        "referral_intent": {
            "keywords": [
                "推荐", "介绍", "朋友推荐", "老客户介绍",
                "转介绍", "推荐有礼", "推荐奖励", "邀请好友",
                "口碑", "好评", "推荐给朋友", "分享给朋友",
                "谁用过", "有人用过吗", "用过的说说",
                "真实评价", "用户评价", "客户案例"
            ],
            "weight": 12
        }
    }
    
    # 购买障碍信号 (负面)
    BARRIER_SIGNALS = {
        "price_concern": {
            "keywords": [
                "太贵", "贵了", "便宜点", "预算不够", "没预算",
                "超出预算", "买不起", "负担不起", "价格太高",
                "有点贵", "能不能便宜", "再便宜点", "价格能不能低",
                "太贵了", "好贵", "价格接受不了", "预算有限",
                "资金紧张", "手头紧", "没那么多钱",
                "价格超出预期", "比预期贵", "比想象中贵",
                "这个价格不行", "价格太高了", "承受不起",
                "经济困难", "资金周转不开", "预算被砍了",
                "没有这笔预算", "预算审批不下来", "费用太高"
            ],
            "weight": -15
        },
        "competitor_mention": {
            "keywords": [
                "别家", "其他公司", "竞品", "对手",
                "别家公司", "其他品牌", "竞品公司", "竞争对手",
                "已经看了别家", "在对比其他", "也在看别家",
                "别人家更便宜", "别家有优惠", "别家送东西",
                "别家功能更多", "别家服务更好", "别家口碑更好",
                "已经定了别家", "准备买别家了", "别家已经报价了",
                "在用别家的", "用过别家的", "别家性价比更高",
                "考虑其他品牌", "看看其他家", "对比了几家"
            ],
            "weight": -10
        },
        "delay_intent": {
            "keywords": [
                "再看看", "考虑一下", "以后再说", "不着急",
                "再想想", "回去考虑", "商量一下", "研究研究",
                "缓一缓", "等一等", "不急用", "暂时不需要",
                "过段时间再说", "以后有需要再联系", "先看看",
                "再对比对比", "再了解了解", "不着急买",
                "还要再想想", "再观望一下", "不急着做决定",
                "先放一放", "以后再说吧", "回头再说",
                "等机会再说", "看情况再说", "再考虑考虑",
                "内部讨论一下", "开会研究一下", "汇报后再说"
            ],
            "weight": -20
        },
        "skepticism": {
            "keywords": [
                "靠谱吗", "真的假的", "骗人", "不信任",
                "可信吗", "真的有用吗", "效果怎么样", "能行吗",
                "靠谱不", "是不是骗人的", "会不会被骗",
                "有保障吗", "有保证吗", "安全吗", "可靠吗",
                "不太相信", "有点怀疑", "持怀疑态度",
                "感觉不太靠谱", "有点担心", "心里没底",
                "不太放心", "有点犹豫", "不太确定",
                "怕被坑", "怕上当", "怕被骗",
                "不敢轻易尝试", "需要再考察", "需要再了解"
            ],
            "weight": -15
        },
        "no_need": {
            "keywords": [
                "不需要", "不用了", "暂时不用", "没需求",
                "用不上", "没必要", "不需要了", "暂时不需要",
                "现在不需要", "以后再说吧", "已经买了",
                "已经有供应商了", "已经合作了", "有其他方案了",
                "自己能做", "内部已经解决了", "有替代方案",
                "暂时不考虑", "目前没这个需求", "需求不强烈",
                "不是刚需", "可有可无", "不是必须的",
                "已经自己开发了", "有免费替代品", "用开源的"
            ],
            "weight": -25
        },
        "authority_issue": {
            "keywords": [
                "我做不了主", "需要请示领导", "要问老板",
                "需要审批", "要汇报", "不是我决定的",
                "领导不同意", "老板不批", "公司没预算",
                "需要上级批准", "要走流程", "需要开会讨论",
                "要报批", "要申请预算", "需要财务审批",
                "不是我负责", "不归我管", "我只是了解下",
                "领导没点头", "老板没同意", "公司政策不允许"
            ],
            "weight": -8
        },
        "timing_issue": {
            "keywords": [
                "现在不方便", "时机不对", "不是时候",
                "年底了", "预算用完了", "下个季度再说",
                "明年再说", "等资金到位",
                "现在不是时候", "时机不成熟", "条件不具备",
                "公司正在调整", "业务在转型", "人员变动",
                "项目暂停了", "计划推迟了", "预算还没批下来",
                "等下一批预算", "等新财年", "等融资到位"
            ],
            "weight": -12
        },
        "feature_concern": {
            "keywords": [
                "功能不够", "不满足需求", "没有我要的功能",
                "功能太少了", "不够用", "缺少功能",
                "不支持", "做不到", "实现不了",
                "功能不完善", "功能有缺陷", "功能不稳定",
                "缺少关键功能", "核心功能没有", "功能不符合",
                "功能太简单", "功能太复杂", "操作太繁琐",
                "界面不好用", "体验不好", "不好上手"
            ],
            "weight": -10
        },
        "service_concern": {
            "keywords": [
                "服务不好", "售后没保障", "没人管",
                "客服态度差", "响应太慢", "处理不及时",
                "服务跟不上", "技术支持不够", "培训不到位",
                "担心售后", "怕没人管", "怕出问题没人解决",
                "服务网点少", "没有本地服务", "远程服务不方便"
            ],
            "weight": -10
        },
        "risk_concern": {
            "keywords": [
                "风险太大", "不确定性太多", "担心出问题",
                "怕亏本", "怕失败", "风险高",
                "投资回报不确定", "收益不明确", "效果不确定",
                "担心数据安全", "担心隐私泄露", "担心系统稳定",
                "怕被绑定", "怕依赖太强", "怕不可控"
            ],
            "weight": -12
        },
        "contract_concern": {
            "keywords": [
                "合同条款不合理", "协议有问题", "条款太苛刻",
                "违约金太高", "责任划分不清", "权益保障不够",
                "合同期限太长", "不能解约", "绑定太死",
                "怕被坑合同", "怕有陷阱", "条款看不懂"
            ],
            "weight": -8
        },
        "integration_concern": {
            "keywords": [
                "系统不兼容", "对接困难", "集成有问题",
                "数据迁移麻烦", "切换成本高", "迁移风险大",
                "现有系统冲突", "技术架构不匹配", "接口不开放",
                "二次开发困难", "定制成本高", "扩展性不好"
            ],
            "weight": -10
        }
    }
    
    # 行为信号权重
    BEHAVIOR_WEIGHTS = {
        "message_frequency_high": 15,      # 消息频率高
        "response_quickly": 10,            # 响应速度快
        "ask_specific_questions": 20,      # 提出具体问题
        "request_details": 15,             # 请求详细信息
        "compare_products": 20,            # 比较产品
        "mention_budget": 25,              # 提及预算
        "ask_payment_method": 20,          # 询问付款方式
    }
    
    def analyze(self, message: str, conversation_history: List[Dict] = None,
                customer_data: Dict = None) -> PurchaseIntentResult:
        """
        分析购买意愿
        
        Args:
            message: 当前消息
            conversation_history: 对话历史
            customer_data: 客户数据
            
        Returns:
            PurchaseIntentResult: 分析结果
        """
        score = 50.0  # 基础分
        signals = []
        barriers = []
        
        message_lower = message.lower()
        
        # 分析购买信号
        for signal_type, config in self.PURCHASE_SIGNALS.items():
            for keyword in config["keywords"]:
                if keyword in message_lower:
                    score += config["weight"]
                    signals.append(f"检测到购买信号: {keyword}")
        
        # 分析购买障碍
        for barrier_type, config in self.BARRIER_SIGNALS.items():
            for keyword in config["keywords"]:
                if keyword in message_lower:
                    score += config["weight"]
                    barriers.append(f"检测到购买障碍: {keyword}")
        
        # 分析对话历史
        if conversation_history:
            history_score = self._analyze_history(conversation_history)
            score += history_score
            
            if history_score > 10:
                signals.append("历史对话显示积极互动")
            elif history_score < -10:
                barriers.append("历史对话显示消极态度")
        
        # 分析客户数据
        if customer_data:
            customer_score = self._analyze_customer_data(customer_data)
            score += customer_score
        
        # 限制分数范围
        score = max(0, min(100, score))
        
        # 确定意愿等级
        level = self._score_to_level(score)
        
        # 生成建议行动
        suggested_action = self._get_suggested_action(level, signals, barriers)
        
        return PurchaseIntentResult(
            level=level,
            score=score,
            signals=signals,
            barriers=barriers,
            suggested_action=suggested_action,
            confidence=min(0.9, 0.5 + len(signals + barriers) * 0.1)
        )
    
    def _analyze_history(self, history: List[Dict]) -> float:
        """分析对话历史"""
        score = 0.0
        
        if not history:
            return score
        
        # 统计用户消息数量
        user_messages = [msg for msg in history if msg.get("direction") == "inbound"]
        if len(user_messages) >= 5:
            score += 10  # 高互动
        elif len(user_messages) >= 3:
            score += 5
        
        # 分析历史消息中的购买信号
        all_content = " ".join([msg.get("content", "") for msg in user_messages])
        
        for signal_type, config in self.PURCHASE_SIGNALS.items():
            for keyword in config["keywords"]:
                if keyword in all_content:
                    score += config["weight"] * 0.3  # 历史信号权重降低
        
        return score
    
    def _analyze_customer_data(self, customer_data: Dict) -> float:
        """分析客户数据"""
        score = 0.0
        
        # 检查标签
        tags = customer_data.get("tags", [])
        if isinstance(tags, list):
            if "high_intent" in tags:
                score += 20
            if "contact_shared" in tags:
                score += 15
            if "demo_requested" in tags:
                score += 10
        
        # 检查意向等级
        intent_level = customer_data.get("intent_level", "")
        if intent_level == "A":
            score += 25
        elif intent_level == "B":
            score += 15
        elif intent_level == "C":
            score += 5
        
        return score
    
    def _score_to_level(self, score: float) -> PurchaseIntentLevel:
        """分数转等级"""
        if score >= 80:
            return PurchaseIntentLevel.VERY_HIGH
        elif score >= 60:
            return PurchaseIntentLevel.HIGH
        elif score >= 40:
            return PurchaseIntentLevel.MEDIUM
        elif score >= 20:
            return PurchaseIntentLevel.LOW
        else:
            return PurchaseIntentLevel.NONE
    
    def _get_suggested_action(self, level: PurchaseIntentLevel, 
                             signals: List[str], barriers: List[str]) -> str:
        """获取建议行动"""
        actions = {
            PurchaseIntentLevel.VERY_HIGH: "立即安排销售跟进，提供专属优惠促成成交",
            PurchaseIntentLevel.HIGH: "优先跟进，发送详细方案和报价",
            PurchaseIntentLevel.MEDIUM: "持续培育，发送案例和产品价值内容",
            PurchaseIntentLevel.LOW: "定期触达，保持品牌曝光",
            PurchaseIntentLevel.NONE: "继续观察，等待更好时机"
        }
        
        base_action = actions.get(level, "继续跟进")
        
        # 根据障碍调整建议
        if barriers:
            if any("价格" in b for b in barriers):
                base_action += "，可考虑提供优惠方案"
            if any("考虑" in b for b in barriers):
                base_action += "，发送更多案例增强信心"
        
        return base_action
