"""
企业知识画像存储与合并服务
"""
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional

from loguru import logger

from src.common.types.knowledge_profile import DomainProfile, FAQSignal, PolicyRule, ProductProfile
from src.infrastructure.runtime_paths import get_data_dir


class DomainProfileService:
    def __init__(self, data_path: Optional[Path] = None):
        base_path = Path(data_path) if data_path else get_data_dir()
        base_path.mkdir(parents=True, exist_ok=True)
        self._profile_path = base_path / "domain_profiles.json"
        self._lock = threading.RLock()
        self._profiles: Dict[str, DomainProfile] = {}
        self._load()

    def _load(self) -> None:
        if not self._profile_path.exists():
            self._profiles = {}
            return
        try:
            data = json.loads(self._profile_path.read_text(encoding="utf-8"))
            self._profiles = {
                str(ent_id): DomainProfile.from_dict(profile_data)
                for ent_id, profile_data in dict(data or {}).items()
            }
        except Exception as exc:
            logger.warning(f"加载知识画像失败: {exc}")
            self._profiles = {}

    def _save(self) -> None:
        payload = {enterprise_id: profile.to_dict() for enterprise_id, profile in self._profiles.items()}
        self._profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_profile(self, enterprise_id: str = "default") -> DomainProfile:
        normalized = str(enterprise_id or "default")
        with self._lock:
            profile = self._profiles.get(normalized)
            if profile:
                return DomainProfile.from_dict(profile.to_dict())
            return DomainProfile(enterprise_id=normalized)

    def upsert_profile(self, profile: DomainProfile) -> DomainProfile:
        normalized = str(profile.enterprise_id or "default")
        with self._lock:
            profile.enterprise_id = normalized
            profile.updated_at = datetime.now().isoformat()
            if not profile.created_at:
                profile.created_at = profile.updated_at
            self._profiles[normalized] = DomainProfile.from_dict(profile.to_dict())
            self._save()
            return self.get_profile(normalized)

    def merge_profile(self, enterprise_id: str, incoming: DomainProfile) -> DomainProfile:
        normalized = str(enterprise_id or "default")
        with self._lock:
            current = self._profiles.get(normalized, DomainProfile(enterprise_id=normalized))
            merged = DomainProfile(
                enterprise_id=normalized,
                industry=incoming.industry if incoming.industry and incoming.industry != "general" else current.industry,
                sub_industry=incoming.sub_industry or current.sub_industry,
                tone=incoming.tone or current.tone,
                summary=self._merge_text(current.summary, incoming.summary),
                products=self._merge_products(current.products, incoming.products),
                policies=self._merge_policies(current.policies, incoming.policies),
                faq_signals=self._merge_faq_signals(current.faq_signals, incoming.faq_signals),
                sales_actions=self._merge_strings(current.sales_actions, incoming.sales_actions),
                forbidden_claims=self._merge_strings(current.forbidden_claims, incoming.forbidden_claims),
                metadata={**current.metadata, **incoming.metadata},
                created_at=current.created_at or incoming.created_at or datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
            )
            self._profiles[normalized] = merged
            self._save()
            return self.get_profile(normalized)

    @staticmethod
    def _merge_text(current: str, incoming: str) -> str:
        incoming = str(incoming or "").strip()
        current = str(current or "").strip()
        if not current:
            return incoming
        if not incoming or incoming in current:
            return current
        if current in incoming:
            return incoming
        return f"{current}\n{incoming}"

    @staticmethod
    def _merge_strings(current: Iterable[str], incoming: Iterable[str]) -> list[str]:
        merged = []
        seen = set()
        for item in list(current or []) + list(incoming or []):
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
        return merged

    def _merge_products(self, current: Iterable[ProductProfile], incoming: Iterable[ProductProfile]) -> list[ProductProfile]:
        merged: Dict[str, ProductProfile] = {}
        for product in list(current or []):
            key = self._product_key(product)
            merged[key] = ProductProfile.from_dict(product.to_dict())
        for product in list(incoming or []):
            key = self._product_key(product)
            existing = merged.get(key)
            if not existing:
                merged[key] = ProductProfile.from_dict(product.to_dict())
                continue
            existing.aliases = self._merge_strings(existing.aliases, product.aliases)
            existing.summary = self._merge_text(existing.summary, product.summary)
            existing.features = self._merge_strings(existing.features, product.features)
            existing.price_notes = self._merge_strings(existing.price_notes, product.price_notes)
            existing.audience = self._merge_strings(existing.audience, product.audience)
            existing.scenarios = self._merge_strings(existing.scenarios, product.scenarios)
            existing.metadata = {**existing.metadata, **product.metadata}
        return list(merged.values())

    def _merge_policies(self, current: Iterable[PolicyRule], incoming: Iterable[PolicyRule]) -> list[PolicyRule]:
        merged: Dict[str, PolicyRule] = {}
        for policy in list(current or []):
            key = self._policy_key(policy)
            merged[key] = PolicyRule.from_dict(policy.to_dict())
        for policy in list(incoming or []):
            key = self._policy_key(policy)
            existing = merged.get(key)
            if not existing:
                merged[key] = PolicyRule.from_dict(policy.to_dict())
                continue
            existing.content = self._merge_text(existing.content, policy.content)
            existing.conditions = self._merge_strings(existing.conditions, policy.conditions)
            existing.priority = max(existing.priority, policy.priority)
            existing.metadata = {**existing.metadata, **policy.metadata}
        return list(merged.values())

    def _merge_faq_signals(self, current: Iterable[FAQSignal], incoming: Iterable[FAQSignal]) -> list[FAQSignal]:
        merged: Dict[str, FAQSignal] = {}
        for signal in list(current or []):
            key = self._faq_key(signal)
            merged[key] = FAQSignal.from_dict(signal.to_dict())
        for signal in list(incoming or []):
            key = self._faq_key(signal)
            existing = merged.get(key)
            if not existing:
                merged[key] = FAQSignal.from_dict(signal.to_dict())
                continue
            existing.keywords = self._merge_strings(existing.keywords, signal.keywords)
            existing.answer_summary = self._merge_text(existing.answer_summary, signal.answer_summary)
            if signal.intent_hint:
                existing.intent_hint = signal.intent_hint
        return list(merged.values())

    @staticmethod
    def _product_key(product: ProductProfile) -> str:
        return str(product.name or "").strip().lower()

    @staticmethod
    def _policy_key(policy: PolicyRule) -> str:
        return f"{str(policy.rule_type or '').strip().lower()}::{str(policy.title or '').strip().lower()}"

    @staticmethod
    def _faq_key(signal: FAQSignal) -> str:
        return str(signal.question or "").strip().lower()


_domain_profile_service: Optional[DomainProfileService] = None


def get_domain_profile_service(data_path: Optional[Path] = None) -> DomainProfileService:
    global _domain_profile_service
    if data_path is not None:
        return DomainProfileService(data_path=data_path)
    if _domain_profile_service is None:
        _domain_profile_service = DomainProfileService()
    return _domain_profile_service


def reset_domain_profile_service() -> None:
    global _domain_profile_service
    _domain_profile_service = None
