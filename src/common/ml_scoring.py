"""
机器学习客户评分模型

基于GitHub最佳实践实现：
- 特征选择：结合行为数据、企业属性、互动历史
- 模型选择：XGBoost/LightGBM
- 实时评分：使用joblib保存模型
- A/B测试：持续优化评分阈值和策略
- 可解释性：使用SHAP值解释模型决策
"""
import os
import json
import joblib
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FeatureSet:
    """特征集合"""
    # 行为特征
    page_views: int = 0
    time_on_site: int = 0
    download_count: int = 0
    form_submissions: int = 0
    
    # 互动特征
    email_opens: int = 0
    email_clicks: int = 0
    chat_sessions: int = 0
    inbound_messages: int = 0
    outbound_messages: int = 0
    
    # 企业特征
    company_size: int = 0
    industry_code: int = 0
    revenue_range: int = 0
    
    # 时间特征
    days_since_first_visit: int = 0
    days_since_last_activity: int = 0
    
    # 衍生特征
    engagement_rate: float = 0.0
    recency_score: float = 0.0
    response_rate: float = 0.0
    
    # 意向特征
    intent_score: float = 0.0
    bant_score: float = 0.0
    
    def to_array(self) -> np.ndarray:
        """转换为numpy数组"""
        return np.array([
            self.page_views,
            self.time_on_site,
            self.download_count,
            self.form_submissions,
            self.email_opens,
            self.email_clicks,
            self.chat_sessions,
            self.inbound_messages,
            self.outbound_messages,
            self.company_size,
            self.industry_code,
            self.revenue_range,
            self.days_since_first_visit,
            self.days_since_last_activity,
            self.engagement_rate,
            self.recency_score,
            self.response_rate,
            self.intent_score,
            self.bant_score
        ]).reshape(1, -1)


@dataclass
class MLPredictionResult:
    """机器学习预测结果"""
    probability: float = 0.0
    score: int = 0
    grade: str = 'D'
    confidence: float = 0.0
    feature_importance: Dict[str, float] = field(default_factory=dict)
    shap_values: Dict[str, float] = field(default_factory=dict)
    top_factors: List[str] = field(default_factory=list)


class FeatureEngineer:
    """
    特征工程器
    
    从原始数据提取和构建特征
    """
    
    # 行业编码映射
    INDUSTRY_CODES = {
        'ecommerce': 1,
        'education': 2,
        'finance': 3,
        'technology': 4,
        'retail': 5,
        'services': 6,
        'healthcare': 7,
        'manufacturing': 8,
        'realestate': 9,
        'unknown': 0
    }
    
    # 企业规模编码
    COMPANY_SIZE_CODES = {
        'enterprise': 4,
        'mid_market': 3,
        'smb': 2,
        'startup': 1,
        'unknown': 0
    }
    
    def extract_features(
        self,
        customer_data: Dict,
        messages: List[Dict] = None,
        behavior_data: Dict = None
    ) -> FeatureSet:
        """
        提取特征
        
        Args:
            customer_data: 客户数据
            messages: 消息历史
            behavior_data: 行为数据
        
        Returns:
            FeatureSet: 特征集合
        """
        features = FeatureSet()
        
        # 从行为数据提取
        if behavior_data:
            features.page_views = behavior_data.get('page_views', 0)
            features.time_on_site = behavior_data.get('time_on_site', 0)
            features.download_count = behavior_data.get('download_count', 0)
            features.form_submissions = behavior_data.get('form_submissions', 0)
            features.email_opens = behavior_data.get('email_opens', 0)
            features.email_clicks = behavior_data.get('email_clicks', 0)
            features.chat_sessions = behavior_data.get('chat_sessions', 0)
        
        # 从消息历史提取
        if messages:
            inbound = [m for m in messages if m.get('direction') == 'inbound']
            outbound = [m for m in messages if m.get('direction') == 'outbound']
            
            features.inbound_messages = len(inbound)
            features.outbound_messages = len(outbound)
            
            # 计算响应率
            if outbound:
                features.response_rate = len(inbound) / len(outbound)
            
            # 计算最近活跃天数
            if inbound:
                last_msg = inbound[-1]
                ts = last_msg.get('created_at') or last_msg.get('timestamp')
                if ts:
                    try:
                        if isinstance(ts, str):
                            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        else:
                            dt = ts
                        features.days_since_last_activity = (datetime.now() - dt.replace(tzinfo=None)).days
                    except Exception:
                        pass
        
        # 从客户数据提取
        if customer_data:
            # 企业规模
            size = customer_data.get('company_size', 'unknown')
            features.company_size = self.COMPANY_SIZE_CODES.get(size, 0)
            
            # 行业
            industry = customer_data.get('industry', 'unknown')
            features.industry_code = self.INDUSTRY_CODES.get(industry, 0)
            
            # 意向分数
            features.intent_score = customer_data.get('intent_score', 0)
            features.bant_score = customer_data.get('bant_score', 0)
        
        # 计算衍生特征
        features.engagement_rate = self._calculate_engagement_rate(features)
        features.recency_score = max(0, 1 - features.days_since_last_activity / 30)
        
        return features
    
    def _calculate_engagement_rate(self, features: FeatureSet) -> float:
        """计算参与度评分"""
        score = (
            features.page_views * 0.1 +
            features.time_on_site / 60 * 0.2 +
            features.download_count * 0.2 +
            features.form_submissions * 0.3 +
            features.chat_sessions * 0.2
        )
        return min(score / 10, 1.0)


class MLScoringModel:
    """
    机器学习评分模型
    
    支持XGBoost/LightGBM/随机森林
    """
    
    FEATURE_NAMES = [
        'page_views', 'time_on_site', 'download_count', 'form_submissions',
        'email_opens', 'email_clicks', 'chat_sessions',
        'inbound_messages', 'outbound_messages',
        'company_size', 'industry_code', 'revenue_range',
        'days_since_first_visit', 'days_since_last_activity',
        'engagement_rate', 'recency_score', 'response_rate',
        'intent_score', 'bant_score'
    ]
    
    def __init__(self, model_type: str = 'lightgbm'):
        """
        初始化模型
        
        Args:
            model_type: 模型类型 (xgboost/lightgbm/random_forest)
        """
        self.model_type = model_type
        self.model = None
        self.is_trained = False
        self.feature_engineer = FeatureEngineer()
        self.model_path = Path('data/models/lead_scoring_model.pkl')
        self.shap_explainer = None
    
    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        params: Dict = None
    ) -> Dict:
        """
        训练模型
        
        Args:
            X: 特征矩阵
            y: 标签 (是否转化)
            params: 模型参数
        
        Returns:
            Dict: 训练结果
        """
        if params is None:
            params = {}
        
        # 根据模型类型创建模型
        if self.model_type == 'xgboost':
            try:
                from xgboost import XGBClassifier
                default_params = {
                    'n_estimators': 100,
                    'max_depth': 5,
                    'learning_rate': 0.1,
                    'subsample': 0.8,
                    'colsample_bytree': 0.8,
                    'random_state': 42,
                    'use_label_encoder': False,
                    'eval_metric': 'logloss'
                }
                default_params.update(params)
                self.model = XGBClassifier(**default_params)
            except ImportError:
                self.model_type = 'random_forest'
        
        if self.model_type == 'lightgbm':
            try:
                from lightgbm import LGBMClassifier
                default_params = {
                    'n_estimators': 100,
                    'max_depth': 5,
                    'learning_rate': 0.1,
                    'random_state': 42,
                    'verbose': -1
                }
                default_params.update(params)
                self.model = LGBMClassifier(**default_params)
            except ImportError:
                self.model_type = 'random_forest'
        
        if self.model_type == 'random_forest':
            from sklearn.ensemble import RandomForestClassifier
            default_params = {
                'n_estimators': 100,
                'max_depth': 5,
                'random_state': 42
            }
            default_params.update(params)
            self.model = RandomForestClassifier(**default_params)
        
        # 训练模型
        self.model.fit(X, y)
        self.is_trained = True
        
        # 保存模型
        self.save_model()
        
        return {
            'model_type': self.model_type,
            'is_trained': True,
            'feature_count': X.shape[1]
        }
    
    def predict(
        self,
        customer_data: Dict,
        messages: List[Dict] = None,
        behavior_data: Dict = None
    ) -> MLPredictionResult:
        """
        预测购买概率
        
        Args:
            customer_data: 客户数据
            messages: 消息历史
            behavior_data: 行为数据
        
        Returns:
            MLPredictionResult: 预测结果
        """
        result = MLPredictionResult()
        
        # 如果模型未训练，使用规则评分
        if not self.is_trained or self.model is None:
            return self._rule_based_predict(customer_data, messages)
        
        # 提取特征
        features = self.feature_engineer.extract_features(
            customer_data, messages, behavior_data
        )
        X = features.to_array()
        
        # 预测概率
        try:
            probability = self.model.predict_proba(X)[0][1]
        except Exception:
            probability = 0.3
        
        result.probability = probability
        result.score = int(probability * 100)
        
        # 评分等级
        if result.score >= 80:
            result.grade = 'A'
        elif result.score >= 60:
            result.grade = 'B'
        elif result.score >= 40:
            result.grade = 'C'
        else:
            result.grade = 'D'
        
        # 特征重要性
        result.feature_importance = self._get_feature_importance()
        
        # SHAP值解释
        result.shap_values = self._get_shap_values(X)
        
        # 关键因素
        result.top_factors = self._get_top_factors(result.shap_values)
        
        result.confidence = self._calculate_confidence(features)
        
        return result
    
    def _rule_based_predict(
        self,
        customer_data: Dict,
        messages: List[Dict]
    ) -> MLPredictionResult:
        """基于规则的预测（模型未训练时的备用方案）"""
        result = MLPredictionResult()
        
        base_score = 30
        
        # 意向等级调整
        intent_level = customer_data.get('intent_level', 'C')
        intent_adjustments = {'A': 40, 'B': 25, 'C': 0, 'D': -15, 'E': -25}
        base_score += intent_adjustments.get(intent_level, 0)
        
        # 消息互动调整
        if messages:
            inbound = [m for m in messages if m.get('direction') == 'inbound']
            base_score += min(len(inbound) * 2, 20)
        
        result.score = max(0, min(100, base_score))
        result.probability = result.score / 100
        
        if result.score >= 80:
            result.grade = 'A'
        elif result.score >= 60:
            result.grade = 'B'
        elif result.score >= 40:
            result.grade = 'C'
        else:
            result.grade = 'D'
        
        result.confidence = 0.5
        result.top_factors = ['意向等级', '互动频率']
        
        return result
    
    def _get_feature_importance(self) -> Dict[str, float]:
        """获取特征重要性"""
        if not self.is_trained or self.model is None:
            return {}
        
        try:
            if hasattr(self.model, 'feature_importances_'):
                importances = self.model.feature_importances_
                return dict(zip(self.FEATURE_NAMES, importances.tolist()))
        except Exception:
            pass
        
        return {}
    
    def _get_shap_values(self, X: np.ndarray) -> Dict[str, float]:
        """
        计算SHAP值
        
        用于解释模型决策
        """
        if not self.is_trained or self.model is None:
            return {}
        
        try:
            import shap
            
            if self.shap_explainer is None:
                if self.model_type == 'xgboost':
                    self.shap_explainer = shap.TreeExplainer(self.model)
                elif self.model_type == 'lightgbm':
                    self.shap_explainer = shap.TreeExplainer(self.model)
                else:
                    self.shap_explainer = shap.KernelExplainer(
                        self.model.predict_proba, 
                        shap.kmeans(X, 10)
                    )
            
            shap_values = self.shap_explainer.shap_values(X)
            
            if isinstance(shap_values, list):
                shap_values = shap_values[1]  # 取正类的SHAP值
            
            return dict(zip(self.FEATURE_NAMES, shap_values[0].tolist()))
            
        except ImportError:
            return {}
        except Exception:
            return {}
    
    def _get_top_factors(self, shap_values: Dict[str, float]) -> List[str]:
        """获取关键影响因素"""
        if not shap_values:
            return []
        
        # 按绝对值排序
        sorted_factors = sorted(
            shap_values.items(),
            key=lambda x: abs(x[1]),
            reverse=True
        )
        
        # 返回前5个因素
        factor_labels = {
            'page_views': '页面浏览',
            'time_on_site': '停留时间',
            'download_count': '下载次数',
            'form_submissions': '表单提交',
            'email_opens': '邮件打开',
            'email_clicks': '邮件点击',
            'chat_sessions': '聊天次数',
            'inbound_messages': '客户消息',
            'outbound_messages': '客服消息',
            'company_size': '企业规模',
            'industry_code': '行业类型',
            'revenue_range': '收入范围',
            'days_since_first_visit': '首次访问',
            'days_since_last_activity': '最近活跃',
            'engagement_rate': '参与度',
            'recency_score': '新鲜度',
            'response_rate': '响应率',
            'intent_score': '意向分',
            'bant_score': 'BANT分'
        }
        
        return [
            f"{factor_labels.get(k, k)}: {'+' if v > 0 else ''}{v:.2f}"
            for k, v in sorted_factors[:5]
        ]
    
    def _calculate_confidence(self, features: FeatureSet) -> float:
        """计算预测置信度"""
        confidence = 0.3
        
        # 数据完整性增加置信度
        if features.inbound_messages > 0:
            confidence += 0.2
        if features.page_views > 0:
            confidence += 0.1
        if features.intent_score > 0:
            confidence += 0.2
        if features.days_since_last_activity < 7:
            confidence += 0.1
        
        return min(confidence, 0.95)
    
    def save_model(self, path: str = None):
        """
        保存模型
        
        Args:
            path: 保存路径
        """
        if path:
            self.model_path = Path(path)
        
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        
        joblib.dump({
            'model': self.model,
            'model_type': self.model_type,
            'is_trained': self.is_trained,
            'feature_names': self.FEATURE_NAMES
        }, self.model_path)
    
    def load_model(self, path: str = None) -> bool:
        """
        加载模型
        
        Args:
            path: 模型路径
        
        Returns:
            bool: 是否加载成功
        """
        if path:
            self.model_path = Path(path)
        
        if not self.model_path.exists():
            return False
        
        try:
            data = joblib.load(self.model_path)
            self.model = data.get('model')
            self.model_type = data.get('model_type', 'random_forest')
            self.is_trained = data.get('is_trained', False)
            return True
        except Exception:
            return False


class ABTestManager:
    """
    A/B测试管理器
    
    用于持续优化评分阈值和策略
    """
    
    def __init__(self):
        self.experiments: Dict[str, Dict] = {}
        self.results: Dict[str, List] = {}
    
    def create_experiment(
        self,
        name: str,
        control_threshold: float,
        variant_threshold: float,
        traffic_split: float = 0.5
    ):
        """
        创建A/B测试实验
        
        Args:
            name: 实验名称
            control_threshold: 对照组阈值
            variant_threshold: 实验组阈值
            traffic_split: 流量分配比例
        """
        self.experiments[name] = {
            'control_threshold': control_threshold,
            'variant_threshold': variant_threshold,
            'traffic_split': traffic_split,
            'created_at': datetime.now().isoformat()
        }
        self.results[name] = []
    
    def assign_variant(self, experiment_name: str, customer_id: str) -> str:
        """
        分配实验变体
        
        Args:
            experiment_name: 实验名称
            customer_id: 客户ID
        
        Returns:
            str: 'control' 或 'variant'
        """
        if experiment_name not in self.experiments:
            return 'control'
        
        # 基于客户ID哈希分配
        hash_value = int(hashlib.md5(f"{experiment_name}_{customer_id}".encode()).hexdigest(), 16)
        split = self.experiments[experiment_name]['traffic_split']
        
        return 'variant' if (hash_value % 100) / 100 < split else 'control'
    
    def get_threshold(self, experiment_name: str, variant: str) -> float:
        """获取实验阈值"""
        if experiment_name not in self.experiments:
            return 50.0
        
        exp = self.experiments[experiment_name]
        return exp['control_threshold'] if variant == 'control' else exp['variant_threshold']
    
    def record_result(
        self,
        experiment_name: str,
        customer_id: str,
        variant: str,
        predicted_score: float,
        actual_outcome: bool
    ):
        """
        记录实验结果
        
        Args:
            experiment_name: 实验名称
            customer_id: 客户ID
            variant: 变体
            predicted_score: 预测分数
            actual_outcome: 实际结果
        """
        if experiment_name not in self.results:
            self.results[experiment_name] = []
        
        self.results[experiment_name].append({
            'customer_id': customer_id,
            'variant': variant,
            'predicted_score': predicted_score,
            'actual_outcome': actual_outcome,
            'timestamp': datetime.now().isoformat()
        })
    
    def analyze_results(self, experiment_name: str) -> Dict:
        """
        分析实验结果
        
        Args:
            experiment_name: 实验名称
        
        Returns:
            Dict: 分析结果
        """
        if experiment_name not in self.results:
            return {'error': '实验不存在'}
        
        results = self.results[experiment_name]
        
        control_results = [r for r in results if r['variant'] == 'control']
        variant_results = [r for r in results if r['variant'] == 'variant']
        
        def calculate_metrics(results_list):
            if not results_list:
                return {'count': 0, 'accuracy': 0, 'precision': 0, 'recall': 0}
            
            tp = sum(1 for r in results_list if r['predicted_score'] >= 50 and r['actual_outcome'])
            fp = sum(1 for r in results_list if r['predicted_score'] >= 50 and not r['actual_outcome'])
            fn = sum(1 for r in results_list if r['predicted_score'] < 50 and r['actual_outcome'])
            tn = sum(1 for r in results_list if r['predicted_score'] < 50 and not r['actual_outcome'])
            
            accuracy = (tp + tn) / len(results_list) if results_list else 0
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            
            return {
                'count': len(results_list),
                'accuracy': accuracy,
                'precision': precision,
                'recall': recall
            }
        
        control_metrics = calculate_metrics(control_results)
        variant_metrics = calculate_metrics(variant_results)
        
        return {
            'experiment_name': experiment_name,
            'control': control_metrics,
            'variant': variant_metrics,
            'winner': 'variant' if variant_metrics['accuracy'] > control_metrics['accuracy'] else 'control'
        }


import hashlib


def get_ml_scoring_model(model_type: str = 'lightgbm') -> MLScoringModel:
    """
    获取机器学习评分模型实例
    
    Args:
        model_type: 模型类型
        
    Returns:
        MLScoringModel: 模型实例
    """
    model = MLScoringModel(model_type=model_type)
    model.load_model()
    return model


def get_ab_test_manager() -> ABTestManager:
    """
    获取A/B测试管理器实例
    
    Returns:
        ABTestManager: 管理器实例
    """
    return ABTestManager()
