"""
系统初始化检查模块

确保服务在重启后能够正常运行，包括：
1. 目录结构检查与创建
2. 配置文件检查与恢复
3. 知识库数据检查
4. 向量数据库检查
5. 数据库文件检查
6. 模型文件检查
"""

import os
import json
import shutil
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from loguru import logger

from src.infrastructure.runtime_paths import (
    get_base_dir,
    get_chroma_dir,
    get_config_dir,
    get_customers_db_path,
    get_data_dir,
    get_knowledge_base_path,
    get_knowledge_files_dir,
    get_log_dir,
    get_models_dir,
)


class SystemInitializer:
    """系统初始化检查器"""
    
    DEFAULT_CONFIG = {
        "llm": {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "model_name": "qwen3.5:4b",
            "max_tokens": 1500,
            "temperature": 0.7,
            "keep_alive": "24h",
            "api_key": "",
            "timeout": 120,
        },
        "embedding": {
            "provider": "local",
            "model_name": "BAAI/bge-small-zh-v1.5",
            "dimension": 512,
            "batch_size": 32,
            "cache_dir": str(get_models_dir() / "embedding"),
        },
        "vector_store": {
            "type": "chroma",
            "persist_directory": str(get_chroma_dir()),
            "collection_prefix": "enterprise_",
            "distance_metric": "cosine"
        },
        "rpa": {
            "poll_interval": 0.5,
            "max_retries": 3,
            "retry_delay": 1.0,
            "headless": False,
            "timeout": 30000,
            "mutation_observer": True,
        },
        "rag": {
            "top_k": 5,
            "enable_reranking": True,
            "use_llm_rerank": True,
            "enable_semantic_dedup": True,
            "dedup_threshold": 0.85,
            "relevance_threshold": 0.3,
            "score_threshold": 0.1,
        },
        "cache": {
            "enabled": True,
            "lru_max_size": 1000,
            "ttl": 300,
            "message_dedup_ttl": 1800,
            "intent_cache_ttl": 180,
        },
        "database": {
            "provider": "sqlite",
            "path": str(get_data_dir() / "huoketest.db"),
            "echo": False,
            "pool_size": 5,
        },
        "redis": {
            "host": "localhost",
            "port": 6379,
            "db": 0,
            "password": "",
        },
        "server": {
            "host": os.getenv("SERVER_HOST") or os.getenv("HOST") or "127.0.0.1",
            "port": int(os.getenv("SERVER_PORT") or os.getenv("PORT") or "8023"),
            "debug": True
        },
        "log": {
            "level": "INFO",
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "file_path": str(get_log_dir() / "app.log"),
            "rotation": "10 MB",
            "retention": "30 days",
            "compression": "zip",
        },
        "version": "2.0.0",
    }
    
    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir or get_base_dir()
        self.data_dir = get_data_dir()
        self.config_dir = get_config_dir()
        self.logs_dir = get_log_dir()
        self.models_dir = get_models_dir()
        self.required_dirs = [
            self.data_dir,
            self.data_dir / "app_state",
            get_chroma_dir(),
            self.data_dir / "feedback",
            self.data_dir / "learning",
            get_knowledge_files_dir(),
            self.logs_dir,
            self.models_dir / "embedding",
            self.models_dir / "embedding" / "reranker",
            self.config_dir,
        ]
        
        self._check_results: List[Dict] = []
    
    def check_all(self) -> Tuple[bool, List[Dict]]:
        """
        执行所有检查
        
        Returns:
            (是否全部通过, 检查结果列表)
        """
        self._check_results = []
        
        self._check_directories()
        self._check_config_files()
        self._check_database()
        self._check_knowledge_base()
        self._check_vector_store()
        self._check_embedding_model()
        
        all_passed = all(r.get("passed", False) for r in self._check_results)
        return all_passed, self._check_results
    
    def _add_result(self, name: str, passed: bool, message: str, action: str = ""):
        """添加检查结果"""
        self._check_results.append({
            "name": name,
            "passed": passed,
            "message": message,
            "action": action
        })
        if passed:
            logger.info(f"[初始化检查] {name}: {message}")
        else:
            logger.warning(f"[初始化检查] {name}: {message} - {action}")
    
    def _check_directories(self):
        """检查并创建必要的目录结构"""
        for full_path in self.required_dirs:
            dir_path = str(full_path.relative_to(self.base_dir)) if full_path.is_relative_to(self.base_dir) else str(full_path)
            if not full_path.exists():
                try:
                    full_path.mkdir(parents=True, exist_ok=True)
                    self._add_result(
                        f"目录:{dir_path}",
                        True,
                        "已创建",
                        "自动创建"
                    )
                except Exception as e:
                    self._add_result(
                        f"目录:{dir_path}",
                        False,
                        f"创建失败: {e}",
                        "请手动创建"
                    )
            else:
                self._add_result(
                    f"目录:{dir_path}",
                    True,
                    "已存在",
                    ""
                )
    
    def _check_config_files(self):
        """检查配置文件"""
        config_file = self.config_dir / "system_config.yaml"
        
        if not config_file.exists():
            try:
                import yaml
                with open(config_file, 'w', encoding='utf-8') as f:
                    yaml.dump(self.DEFAULT_CONFIG, f, allow_unicode=True, default_flow_style=False)
                self._add_result(
                    "配置文件:system_config.yaml",
                    True,
                    "已创建默认配置",
                    "自动创建"
                )
            except Exception as e:
                self._add_result(
                    "配置文件:system_config.yaml",
                    False,
                    f"创建失败: {e}",
                    "请检查yaml模块或手动创建"
                )
        else:
            self._add_result(
                "配置文件:system_config.yaml",
                True,
                "已存在",
                ""
            )
    
    def _check_database(self):
        """检查数据库文件"""
        db_file = get_customers_db_path()
        
        if not db_file.exists():
            try:
                default_data = {
                    "customers": [],
                    "platforms": {},
                    "send_modes": {},
                    "messages": [],
                    "conversations": {}
                }
                with open(db_file, 'w', encoding='utf-8') as f:
                    json.dump(default_data, f, ensure_ascii=False, indent=2)
                self._add_result(
                    "数据库:customers.json",
                    True,
                    "已创建空数据库",
                    "自动创建"
                )
            except Exception as e:
                self._add_result(
                    "数据库:customers.json",
                    False,
                    f"创建失败: {e}",
                    "请手动创建"
                )
        else:
            try:
                with open(db_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if "customers" not in data:
                    data["customers"] = []
                if "platforms" not in data:
                    data["platforms"] = {}
                if "send_modes" not in data:
                    data["send_modes"] = {}
                with open(db_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._add_result(
                    "数据库:customers.json",
                    True,
                    "已存在且结构正确",
                    ""
                )
            except Exception as e:
                self._add_result(
                    "数据库:customers.json",
                    False,
                    f"读取失败: {e}",
                    "请检查文件格式"
                )
    
    def _check_knowledge_base(self):
        """检查知识库数据"""
        kb_file = get_knowledge_base_path()
        
        if not kb_file.exists():
            self._add_result(
                "知识库:knowledge_base.json",
                False,
                "知识库文件不存在",
                "请运行知识库生成脚本"
            )
        else:
            try:
                with open(kb_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                count = len(data) if isinstance(data, list) else 0
                if count > 0:
                    self._add_result(
                        "知识库:knowledge_base.json",
                        True,
                        f"已存在，包含{count}条知识",
                        ""
                    )
                else:
                    self._add_result(
                        "知识库:knowledge_base.json",
                        False,
                        "知识库为空",
                        "请运行知识库生成脚本"
                    )
            except Exception as e:
                self._add_result(
                    "知识库:knowledge_base.json",
                    False,
                    f"读取失败: {e}",
                    "请检查文件格式"
                )
    
    def _check_vector_store(self):
        """检查向量数据库"""
        chroma_dir = get_chroma_dir()
        
        if not chroma_dir.exists():
            self._add_result(
                "向量库:chroma_db",
                False,
                "向量数据库目录不存在",
                "首次启动时会自动创建"
            )
        else:
            collections = list(chroma_dir.glob("*"))
            if collections:
                self._add_result(
                    "向量库:chroma_db",
                    True,
                    f"已存在，包含{len(collections)}个集合",
                    ""
                )
            else:
                self._add_result(
                    "向量库:chroma_db",
                    False,
                    "向量数据库为空",
                    "首次启动时会自动初始化"
                )
    
    def _check_embedding_model(self):
        """检查Embedding模型"""
        candidate_dirs = [
            self.models_dir / "embedding" / "bge-small-zh-v1.5",
            self.models_dir / "embedding" / "BAAI" / "bge-small-zh-v1.5",
        ]
        model_dir = None
        for candidate in candidate_dirs:
            if candidate.exists() and candidate.is_dir():
                model_dir = candidate
                break
        if model_dir is None:
            model_dir = candidate_dirs[0]

        required_files = ["config.json", "model.safetensors", "tokenizer.json", "vocab.txt"]
        missing_files = []

        for f in required_files:
            if not (model_dir / f).exists():
                missing_files.append(f)

        if missing_files:
            self._add_result(
                "Embedding模型:bge-small-zh-v1.5",
                False,
                f"缺少文件: {', '.join(missing_files)}",
                "首次使用时会自动下载"
            )
        else:
            self._add_result(
                "Embedding模型:bge-small-zh-v1.5",
                True,
                "模型文件完整",
                ""
            )
    
    def fix_issues(self) -> bool:
        """
        尝试自动修复问题
        
        Returns:
            是否修复成功
        """
        all_passed, results = self.check_all()
        
        if all_passed:
            logger.info("[系统初始化] 所有检查通过，无需修复")
            return True
        
        logger.info("[系统初始化] 开始自动修复...")
        
        for result in results:
            if not result.get("passed", False):
                logger.warning(f"[系统初始化] 无法自动修复: {result['name']} - {result['message']}")
        
        all_passed, _ = self.check_all()
        return all_passed


def ensure_system_ready() -> bool:
    """
    确保系统准备就绪
    
    Returns:
        系统是否准备就绪
    """
    initializer = SystemInitializer()
    passed, results = initializer.check_all()
    
    if not passed:
        logger.warning("[系统初始化] 部分检查未通过，请查看详情:")
        for r in results:
            if not r.get("passed", False):
                logger.warning(f"  - {r['name']}: {r['message']} ({r['action']})")
    
    return passed


def get_system_status() -> Dict:
    """
    获取系统状态
    
    Returns:
        系统状态字典
    """
    initializer = SystemInitializer()
    passed, results = initializer.check_all()
    
    return {
        "ready": passed,
        "checks": results,
        "base_dir": str(initializer.base_dir),
        "data_dir": str(initializer.data_dir),
        "config_dir": str(initializer.config_dir)
    }


if __name__ == "__main__":
    import sys
    
    initializer = SystemInitializer()
    passed, results = initializer.check_all()
    
    print("\n" + "=" * 60)
    print("系统初始化检查报告")
    print("=" * 60)
    
    for r in results:
        status = "✅" if r["passed"] else "❌"
        print(f"{status} {r['name']}: {r['message']}")
        if r["action"]:
            print(f"   → {r['action']}")
    
    print("=" * 60)
    if passed:
        print("✅ 系统准备就绪")
        sys.exit(0)
    else:
        print("❌ 系统未准备就绪，请检查上述问题")
        sys.exit(1)
