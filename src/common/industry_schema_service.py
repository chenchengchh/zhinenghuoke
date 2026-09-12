from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from src.common.domain_profile_service import get_domain_profile_service

from src.infrastructure.runtime_paths import get_base_dir, get_industry_schemas_dir


class IndustrySchemaService:
    """行业 Schema 配置服务。"""
    DEFAULT_SCHEMA_ID = "generic.service_sales"

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_path = Path(base_dir) if base_dir else get_industry_schemas_dir()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.base_path / "settings.json"
        self._bootstrap_defaults()

    def get_default_settings(self) -> Dict[str, Any]:
        return {
            "active_schema_id": self.DEFAULT_SCHEMA_ID,
            "default_resolution_policy": "fail_closed",
            "require_enterprise_binding": True,
            "fallback_schema_id": self.DEFAULT_SCHEMA_ID,
            "allow_preferred_schema_override": True,
            "enterprise_schema_bindings": {},
        }

    def _get_bundled_schema_dir(self) -> Path | None:
        candidates = [
            get_base_dir() / "_internal" / "data" / "industry_schemas",
            get_base_dir() / "data" / "industry_schemas",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _bootstrap_defaults(self) -> None:
        bundled_dir = self._get_bundled_schema_dir()
        if bundled_dir:
            for source_path in bundled_dir.glob("*.json"):
                if source_path.name == "settings.json":
                    continue
                target_path = self.base_path / source_path.name
                if not target_path.exists():
                    target_path.write_text(
                        source_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
        if not self.settings_path.exists():
            self._write_json(self.settings_path, self.get_default_settings())

    def _schema_path(self, schema_id: str) -> Path:
        safe_name = str(schema_id or "").strip().replace("/", "__").replace("\\", "__")
        if not safe_name:
            raise ValueError("schema_id 不能为空")
        return self.base_path / f"{safe_name}.json"

    def _read_json(self, path: Path) -> Dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_settings(self) -> Dict[str, Any]:
        defaults = self.get_default_settings()
        if not self.settings_path.exists():
            return deepcopy(defaults)

        raw = self._read_json(self.settings_path)
        if not isinstance(raw, dict):
            return deepcopy(defaults)

        merged = deepcopy(defaults)
        for key, value in raw.items():
            if key == "enterprise_schema_bindings":
                if isinstance(value, dict):
                    merged[key] = dict(value)
                continue
            merged[key] = value
        return merged

    def get_default_schema_id(self) -> str:
        return self.DEFAULT_SCHEMA_ID

    def _normalize_schema_id(self, schema_id: str) -> str:
        return str(schema_id or "").strip()

    def _schema_exists(self, schema_id: str) -> bool:
        normalized = self._normalize_schema_id(schema_id)
        if not normalized:
            return False
        try:
            self.get_schema(normalized)
            return True
        except FileNotFoundError:
            return False

    def _get_bound_schema_id_from_profile(self, enterprise_id: str = "") -> str:
        normalized_enterprise = str(enterprise_id or "").strip()
        if not normalized_enterprise:
            return ""
        try:
            profile = get_domain_profile_service().get_profile(normalized_enterprise)
            return self._normalize_schema_id((profile.metadata or {}).get("schema_id"))
        except Exception:
            return ""

    def _get_bound_schema_id_from_settings(self, enterprise_id: str = "") -> str:
        normalized_enterprise = str(enterprise_id or "").strip()
        if not normalized_enterprise:
            return ""
        settings = self.get_settings()
        bindings = settings.get("enterprise_schema_bindings") or {}
        if not isinstance(bindings, dict):
            return ""
        return self._normalize_schema_id(bindings.get(normalized_enterprise))

    def _normalize_resolution_mode(self, resolution_mode: str = "") -> str:
        normalized = str(resolution_mode or "").strip().lower()
        if normalized in {"required", "fail_closed"}:
            return "fail_closed"
        if normalized in {"allow_default", "fallback_generic", "generic"}:
            return "fallback_generic"

        settings_mode = str(
            self.get_settings().get("default_resolution_policy") or ""
        ).strip().lower()
        if settings_mode in {"fallback_generic", "generic"}:
            return "fallback_generic"
        return "fail_closed"

    def _get_fallback_schema_id(self) -> str:
        settings = self.get_settings()
        candidate = self._normalize_schema_id(settings.get("fallback_schema_id"))
        if candidate and self._schema_exists(candidate):
            return candidate
        if self._schema_exists(self.DEFAULT_SCHEMA_ID):
            return self.DEFAULT_SCHEMA_ID
        return ""

    def resolve_schema_id(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> str:
        settings = self.get_settings()
        normalized_enterprise = str(enterprise_id or "").strip()
        normalized_preferred = self._normalize_schema_id(preferred_schema_id)
        effective_mode = self._normalize_resolution_mode(resolution_mode)

        allow_preferred_override = bool(
            settings.get("allow_preferred_schema_override", True)
        )
        require_enterprise_binding = bool(
            settings.get("require_enterprise_binding", False)
        )

        candidates = []
        if allow_preferred_override and normalized_preferred:
            candidates.append(normalized_preferred)
        candidates.append(self._get_bound_schema_id_from_profile(normalized_enterprise))
        candidates.append(self._get_bound_schema_id_from_settings(normalized_enterprise))

        for candidate in candidates:
            if candidate and self._schema_exists(candidate):
                return candidate

        if require_enterprise_binding and not normalized_enterprise:
            if effective_mode == "fail_closed":
                return ""

        if require_enterprise_binding and normalized_enterprise:
            if not any(candidate for candidate in candidates):
                if effective_mode == "fail_closed":
                    return ""

        if effective_mode == "fallback_generic":
            fallback_schema_id = self._get_fallback_schema_id()
            if fallback_schema_id:
                return fallback_schema_id

        active_schema_id = self._normalize_schema_id(settings.get("active_schema_id"))
        if active_schema_id and self._schema_exists(active_schema_id):
            return active_schema_id

        if self._schema_exists(self.DEFAULT_SCHEMA_ID):
            return self.DEFAULT_SCHEMA_ID
        return ""

    def preview_schema_resolution(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> Dict[str, Any]:
        settings = self.get_settings()
        normalized_enterprise = str(enterprise_id or "").strip()
        normalized_preferred = self._normalize_schema_id(preferred_schema_id)
        effective_mode = self._normalize_resolution_mode(resolution_mode)

        allow_preferred_override = bool(
            settings.get("allow_preferred_schema_override", True)
        )
        require_enterprise_binding = bool(
            settings.get("require_enterprise_binding", False)
        )
        profile_binding = self._get_bound_schema_id_from_profile(normalized_enterprise)
        settings_binding = self._get_bound_schema_id_from_settings(normalized_enterprise)

        if allow_preferred_override and normalized_preferred and self._schema_exists(normalized_preferred):
            return {
                "resolved_schema_id": normalized_preferred,
                "source": "preferred_schema_override",
                "effective_resolution_mode": effective_mode,
                "enterprise_id": normalized_enterprise,
                "preferred_schema_id": normalized_preferred,
                "details": {
                    "profile_binding": profile_binding,
                    "settings_binding": settings_binding,
                    "require_enterprise_binding": require_enterprise_binding,
                    "allow_preferred_schema_override": allow_preferred_override,
                },
            }

        if profile_binding and self._schema_exists(profile_binding):
            return {
                "resolved_schema_id": profile_binding,
                "source": "enterprise_profile_binding",
                "effective_resolution_mode": effective_mode,
                "enterprise_id": normalized_enterprise,
                "preferred_schema_id": normalized_preferred,
                "details": {
                    "profile_binding": profile_binding,
                    "settings_binding": settings_binding,
                    "require_enterprise_binding": require_enterprise_binding,
                    "allow_preferred_schema_override": allow_preferred_override,
                },
            }

        if settings_binding and self._schema_exists(settings_binding):
            return {
                "resolved_schema_id": settings_binding,
                "source": "settings_binding",
                "effective_resolution_mode": effective_mode,
                "enterprise_id": normalized_enterprise,
                "preferred_schema_id": normalized_preferred,
                "details": {
                    "profile_binding": profile_binding,
                    "settings_binding": settings_binding,
                    "require_enterprise_binding": require_enterprise_binding,
                    "allow_preferred_schema_override": allow_preferred_override,
                },
            }

        if require_enterprise_binding and not normalized_enterprise and effective_mode == "fail_closed":
            return {
                "resolved_schema_id": "",
                "source": "fail_closed_missing_enterprise",
                "effective_resolution_mode": effective_mode,
                "enterprise_id": normalized_enterprise,
                "preferred_schema_id": normalized_preferred,
                "details": {
                    "profile_binding": profile_binding,
                    "settings_binding": settings_binding,
                    "require_enterprise_binding": require_enterprise_binding,
                    "allow_preferred_schema_override": allow_preferred_override,
                },
            }

        if require_enterprise_binding and normalized_enterprise and not any(candidate for candidate in (profile_binding, settings_binding)):
            if effective_mode == "fail_closed":
                return {
                    "resolved_schema_id": "",
                    "source": "fail_closed_missing_binding",
                    "effective_resolution_mode": effective_mode,
                    "enterprise_id": normalized_enterprise,
                    "preferred_schema_id": normalized_preferred,
                    "details": {
                        "profile_binding": profile_binding,
                        "settings_binding": settings_binding,
                        "require_enterprise_binding": require_enterprise_binding,
                        "allow_preferred_schema_override": allow_preferred_override,
                    },
                }

        if effective_mode == "fallback_generic":
            fallback_schema_id = self._get_fallback_schema_id()
            if fallback_schema_id:
                return {
                    "resolved_schema_id": fallback_schema_id,
                    "source": "fallback_schema",
                    "effective_resolution_mode": effective_mode,
                    "enterprise_id": normalized_enterprise,
                    "preferred_schema_id": normalized_preferred,
                    "details": {
                        "profile_binding": profile_binding,
                        "settings_binding": settings_binding,
                        "require_enterprise_binding": require_enterprise_binding,
                        "allow_preferred_schema_override": allow_preferred_override,
                    },
                }

        active_schema_id = self._normalize_schema_id(settings.get("active_schema_id"))
        if active_schema_id and self._schema_exists(active_schema_id):
            return {
                "resolved_schema_id": active_schema_id,
                "source": "active_schema",
                "effective_resolution_mode": effective_mode,
                "enterprise_id": normalized_enterprise,
                "preferred_schema_id": normalized_preferred,
                "details": {
                    "profile_binding": profile_binding,
                    "settings_binding": settings_binding,
                    "require_enterprise_binding": require_enterprise_binding,
                    "allow_preferred_schema_override": allow_preferred_override,
                },
            }

        if self._schema_exists(self.DEFAULT_SCHEMA_ID):
            return {
                "resolved_schema_id": self.DEFAULT_SCHEMA_ID,
                "source": "default_schema",
                "effective_resolution_mode": effective_mode,
                "enterprise_id": normalized_enterprise,
                "preferred_schema_id": normalized_preferred,
                "details": {
                    "profile_binding": profile_binding,
                    "settings_binding": settings_binding,
                    "require_enterprise_binding": require_enterprise_binding,
                    "allow_preferred_schema_override": allow_preferred_override,
                },
            }

        return {
            "resolved_schema_id": "",
            "source": "unresolved",
            "effective_resolution_mode": effective_mode,
            "enterprise_id": normalized_enterprise,
            "preferred_schema_id": normalized_preferred,
            "details": {
                "profile_binding": profile_binding,
                "settings_binding": settings_binding,
                "require_enterprise_binding": require_enterprise_binding,
                "allow_preferred_schema_override": allow_preferred_override,
            },
        }

    def get_effective_active_schema_id(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> str:
        return self.resolve_schema_id(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            resolution_mode=resolution_mode,
        )

    def get_active_schema(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> Dict[str, Any]:
        active_schema_id = self.get_effective_active_schema_id(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            resolution_mode=resolution_mode,
        )
        if not active_schema_id:
            return {}
        try:
            return self.get_schema(active_schema_id)
        except FileNotFoundError:
            return {}

    def bind_enterprise_schema(self, enterprise_id: str, schema_id: str) -> Dict[str, Any]:
        normalized_enterprise = str(enterprise_id or "").strip()
        normalized_schema = self.resolve_schema_id(preferred_schema_id=schema_id)
        if not normalized_enterprise:
            raise ValueError("enterprise_id 不能为空")
        settings = self.get_settings()
        bindings = settings.get("enterprise_schema_bindings") or {}
        if not isinstance(bindings, dict):
            bindings = {}
        bindings[normalized_enterprise] = normalized_schema
        settings["enterprise_schema_bindings"] = bindings
        self._write_json(self.settings_path, settings)
        return settings

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_settings()
        incoming = updates or {}
        for key, value in incoming.items():
            if key == "enterprise_schema_bindings":
                if isinstance(value, dict):
                    current[key] = dict(value)
                continue
            current[key] = value
        self._write_json(self.settings_path, current)
        return current

    def list_schemas(self) -> List[Dict[str, Any]]:
        active_schema_id = self.get_settings().get("active_schema_id", "")
        items: List[Dict[str, Any]] = []
        for path in sorted(self.base_path.glob("*.json")):
            if path.name == self.settings_path.name:
                continue
            data = self._read_json(path)
            items.append(
                {
                    "schema_id": data.get("schema_id") or path.stem,
                    "display_name": data.get("display_name") or data.get("schema_id") or path.stem,
                    "industry_code": data.get("industry_code", ""),
                    "entity_type": data.get("entity_type", ""),
                    "version": data.get("version", 1),
                    "field_count": len(data.get("fields") or []),
                    "intent_count": len(data.get("intent_keywords") or {}),
                    "is_active": (data.get("schema_id") or path.stem) == active_schema_id,
                    "description": data.get("description", ""),
                }
            )
        return items

    def get_schema(self, schema_id: str) -> Dict[str, Any]:
        path = self._schema_path(schema_id)
        if not path.exists():
            raise FileNotFoundError(schema_id)
        return self._read_json(path)

    def save_schema(self, schema_id: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        payload = deepcopy(schema or {})
        payload["schema_id"] = schema_id
        self.validate_schema(payload)
        self._write_json(self._schema_path(schema_id), payload)
        return payload

    def delete_schema(self, schema_id: str) -> Dict[str, Any]:
        normalized_schema_id = self._normalize_schema_id(schema_id)
        if not normalized_schema_id:
            raise ValueError("schema_id 不能为空")

        settings = self.get_settings()
        active_schema_id = self._normalize_schema_id(settings.get("active_schema_id"))
        fallback_schema_id = self._normalize_schema_id(settings.get("fallback_schema_id"))
        if normalized_schema_id == active_schema_id:
            raise ValueError("当前启用中的模板不能直接删除，请先切换到其他模板")
        if normalized_schema_id == fallback_schema_id:
            raise ValueError("当前回退模板不能直接删除，请先修改回退模板")

        path = self._schema_path(normalized_schema_id)
        if not path.exists():
            raise FileNotFoundError(normalized_schema_id)

        if not self._normalize_schema_id(path.stem) == normalized_schema_id:
            raise ValueError("只允许删除标准行业 Schema 文件")

        path.unlink()

        bindings = settings.get("enterprise_schema_bindings") or {}
        if isinstance(bindings, dict):
            next_bindings = {
                str(enterprise_id or "").strip(): str(bound_schema_id or "").strip()
                for enterprise_id, bound_schema_id in bindings.items()
                if str(enterprise_id or "").strip()
                and str(bound_schema_id or "").strip()
                and self._normalize_schema_id(bound_schema_id) != normalized_schema_id
            }
            if next_bindings != bindings:
                settings["enterprise_schema_bindings"] = next_bindings
                self._write_json(self.settings_path, settings)

        return {
            "deleted_schema_id": normalized_schema_id,
            "removed_bindings": [
                enterprise_id
                for enterprise_id, bound_schema_id in (bindings.items() if isinstance(bindings, dict) else [])
                if self._normalize_schema_id(bound_schema_id) == normalized_schema_id
            ],
        }

    def get_schema_template(self) -> Dict[str, Any]:
        return {
            "schema_id": "new.industry_schema",
            "version": 1,
            "display_name": "新行业 Schema",
            "industry_code": "generic",
            "entity_type": "product",
            "description": "请根据目标行业补充实体字段、意图关键词和 grounded 配置。",
            "fields": [
                {
                    "name": "name",
                    "label": "名称",
                    "type": "string",
                    "retrievable": True,
                    "comparable": True,
                },
                {
                    "name": "price",
                    "label": "价格",
                    "type": "string",
                    "retrievable": True,
                    "comparable": True,
                },
            ],
            "intent_keywords": {
                "price": ["价格", "多少钱", "费用"],
                "comparison": ["区别", "对比", "怎么选"],
            },
            "grounded_config": {
                "bundle_type": "entity",
                "comparison_fields": ["price"],
                "followup_fields": ["price", "details"],
            },
            "metadata": {
                "ui_hint": "运营可直接在前端编辑此 JSON；后续策略模块会按 schema_id 动态装配。",
                "category_profiles": {
                    "product": {"label": "产品介绍", "keywords": ["产品", "方案", "功能", "服务包"]},
                    "service": {"label": "服务流程", "keywords": ["服务", "客服", "售后", "支持", "咨询"]},
                    "price": {"label": "价格费用", "keywords": ["价格", "费用", "多少钱", "报价", "预算"]},
                    "process": {"label": "流程说明", "keywords": ["流程", "步骤", "如何开通", "怎么使用", "部署"]},
                    "faq": {"label": "常见问题", "keywords": ["如何", "怎么", "为什么", "可以吗", "是否"]},
                    "policy": {"label": "规则政策", "keywords": ["规则", "政策", "退款", "协议", "条款"]},
                    "company": {"label": "公司介绍", "keywords": ["公司", "团队", "品牌", "资质", "案例"]},
                    "other": {"label": "其他", "keywords": []}
                },
                    "query_understanding": {
                        "domain_keywords": [],
                        "business_intro_terms": [],
                        "business_intro_patterns": [],
                        "business_intro_exact_phrases": [],
                        "followup_terms": [],
                        "contextual_opening_guidance": []
                    },
                    "intent_recognition": {
                        "domain_context_terms": [],
                        "domain_intent_augments": {},
                        "domain_only_intent_patterns": {},
                        "domain_entity_patterns": {},
                        "domain_negation_rules": {},
                        "domain_intent_inheritance": {},
                        "domain_intent_entity_map": {},
                        "domain_intent_descriptions": {},
                        "domain_reasoning_names": {}
                    },
                    "followup_strategy": {
                        "domain_keywords": [],
                        "generic_patterns": [],
                        "anchor_terms": [],
                        "short_followup_clues": [],
                        "short_followup_leads": [],
                        "required_slots": {},
                        "slot_priority": {},
                        "known_slot_terms": {},
                        "signal_terms": {},
                        "followup_intent_terms": {},
                        "custom_rewrite_rules": [],
                        "stage_cta_templates": {},
                        "clarification_templates": {},
                        "grounded_plan_closing_templates": {},
                        "single_route_closing_templates": {}
                    },
                    "retrieval_policy": {
                        "intent_category_map": {},
                        "intent_term_hints": {},
                        "enabled_sources": {},
                        "per_source_top_k": {},
                        "query_type_terms": {},
                        "scoring": {
                            "source_weights": {},
                            "query_type_source_weights": {},
                            "contact_pollution_weights": {},
                            "query_category_weights": {},
                            "domain_route_weights": {},
                            "exact_match": {}
                        }
                    },
                    "reply_policy": {
                        "eligibility_thresholds": {
                            "min_send_confidence": 0.8,
                            "min_grounded_confidence": 0.7,
                            "allow_no_answer_fail_soft": False,
                            "enable_ack_only": True,
                            "high_risk_action": "pause_for_human",
                            "complaint_action": "pause_for_human",
                            "quota_block_action": "retry_later",
                            "unknown_direction_action": "skip"
                        },
                        "intent_reply_fallbacks": {},
                        "stage_objectives": {},
                        "cta_append_policy": {
                            "disable_when_next_action": [],
                            "disable_when_cta_mode": [],
                            "fact_query_block_terms": [],
                            "explicit_contact_terms": []
                        }
                    },
                    "slot_questions": {},
                "reply_profile": {
                    "sales_persona": "你是一名行业顾问，基于知识库为客户解释方案差异、适用条件和下一步建议。",
                    "qa_persona": "你是一名专业顾问，只回答当前行业相关问题。",
                    "sales_focus": "优先回答客户当前问题，再自然补充完成判断所需的关键信息。",
                    "domain_rules": [
                        "只使用知识库中提供的信息，不要编造未提供的事实。",
                        "先答当前问题，再推进下一步。"
                    ],
                    "base_replies": {
                        "greeting": "",
                            "business_intro": "",
                        "default": "",
                        "fallback": ""
                    }
                }
            },
        }

    def validate_schema(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        payload = schema or {}
        required = ["schema_id", "display_name", "industry_code", "entity_type"]
        missing = [key for key in required if not str(payload.get(key, "")).strip()]
        if missing:
            raise ValueError(f"缺少必填字段: {', '.join(missing)}")

        fields = payload.get("fields") or []
        if not isinstance(fields, list):
            raise ValueError("fields 必须是数组")
        for idx, field_item in enumerate(fields):
            if not isinstance(field_item, dict):
                raise ValueError(f"fields[{idx}] 必须是对象")
            if not str(field_item.get("name", "")).strip():
                raise ValueError(f"fields[{idx}].name 不能为空")

        intent_keywords = payload.get("intent_keywords") or {}
        if not isinstance(intent_keywords, dict):
            raise ValueError("intent_keywords 必须是对象")

        grounded_config = payload.get("grounded_config") or {}
        if not isinstance(grounded_config, dict):
            raise ValueError("grounded_config 必须是对象")

        metadata = payload.get("metadata") or {}
        if metadata and not isinstance(metadata, dict):
            raise ValueError("metadata 必须是对象")
        category_profiles = metadata.get("category_profiles") or {}
        if category_profiles and not isinstance(category_profiles, dict):
            raise ValueError("metadata.category_profiles 必须是对象")

        return {
            "valid": True,
            "schema_id": payload.get("schema_id"),
            "field_count": len(fields),
            "intent_count": len(intent_keywords),
        }


@lru_cache(maxsize=1)
def get_industry_schema_service() -> IndustrySchemaService:
    return IndustrySchemaService()


def get_active_schema_with_compat(
    service: Any,
    *,
    enterprise_id: str = "",
    preferred_schema_id: str = "",
    resolution_mode: str = "",
) -> Dict[str, Any]:
    if service is None or not hasattr(service, "get_active_schema"):
        return {}
    try:
        return service.get_active_schema(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            resolution_mode=resolution_mode,
        ) or {}
    except TypeError:
        if any(
            str(value or "").strip()
            for value in (enterprise_id, preferred_schema_id, resolution_mode)
        ):
            return {}
        try:
            return service.get_active_schema() or {}
        except Exception:
            return {}
    except Exception:
        return {}
