import json
import logging
from datetime import datetime
from typing import Dict, Any
from copy import deepcopy

from src.infrastructure.runtime_paths import get_data_dir

logger = logging.getLogger(__name__)

DEFAULT_CRAWL_LIMIT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "daily_limit": 8000,
}

DEFAULT_CRAWL_LIMIT_STATE: Dict[str, Any] = {
    "config": deepcopy(DEFAULT_CRAWL_LIMIT_CONFIG),
    "records": {},  # "YYYY-MM-DD": count
}

class CrawlLimitService:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, state_path=None):
        if not hasattr(self, "_initialized"):
            self._state_path = state_path or get_data_dir() / "crawl_limit.json"
            self._state = self._load_state()
            self._initialized = True

    def _load_state(self) -> dict:
        if not self._state_path.exists():
            return deepcopy(DEFAULT_CRAWL_LIMIT_STATE)
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                state = deepcopy(DEFAULT_CRAWL_LIMIT_STATE)
                if isinstance(data, dict):
                    if "config" in data and isinstance(data["config"], dict):
                        state["config"].update(data["config"])
                    if "records" in data and isinstance(data["records"], dict):
                        state["records"] = data["records"]
                return state
        except Exception as e:
            logger.error(f"加载爬取限制配置失败: {e}")
            return deepcopy(DEFAULT_CRAWL_LIMIT_STATE)

    def _save_state(self) -> bool:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            logger.error(f"保存爬取限制配置失败: {e}")
            return False

    def get_config(self) -> dict:
        return deepcopy(self._state["config"])

    def update_config(self, updates: dict) -> bool:
        if "enabled" in updates:
            self._state["config"]["enabled"] = bool(updates["enabled"])
        if "daily_limit" in updates:
            self._state["config"]["daily_limit"] = int(updates["daily_limit"])
        return self._save_state()

    def get_today_count(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return self._state["records"].get(today, 0)

    def increment_today_count(self, count: int = 1) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        current = self._state["records"].get(today, 0)
        self._state["records"][today] = current + count
        self._save_state()

    def check_limit(self) -> dict:
        """
        检查是否触发了每日爬取限制
        """
        config = self._state["config"]
        if not config.get("enabled", True):
            return {
                "limited": False,
                "reason": "爬取限制未开启",
                "today_scanned": self.get_today_count(),
                "daily_limit": config.get("daily_limit", 8000)
            }
            
        daily_limit = config.get("daily_limit", 8000)
        today_count = self.get_today_count()
        
        limited = today_count >= daily_limit
        return {
            "limited": limited,
            "reason": f"今日爬取量已达上限 ({today_count}/{daily_limit})" if limited else "",
            "today_scanned": today_count,
            "daily_limit": daily_limit
        }

def get_crawl_limit_service() -> CrawlLimitService:
    return CrawlLimitService()
