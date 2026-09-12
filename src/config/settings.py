import os

from src.infrastructure.env_loader import load_project_env
from src.infrastructure.runtime_paths import (
    ensure_runtime_directories,
    get_base_dir,
    get_browser_user_data_dir,
    get_crawler_browser_user_data_dir,
    get_customers_db_path,
    get_data_dir,
    get_log_dir,
)

load_project_env(base_dir=get_base_dir(), override=False)


def _get_env(*keys: str, default: str) -> str:
    for key in keys:
        value = os.getenv(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default

# Base Paths
BASE_DIR = get_base_dir()
DATA_DIR = get_data_dir()
LOG_DIR = get_log_dir()
USER_DATA_DIR = get_browser_user_data_dir()
CRAWLER_USER_DATA_DIR = get_crawler_browser_user_data_dir()

# Ensure directories exist
ensure_runtime_directories()

# Douyin URLs
DOUYIN_HOME_URL = os.getenv("DOUYIN_HOME_URL", "https://www.douyin.com")
DOUYIN_CHAT_URL = os.getenv("DOUYIN_CHAT_URL", "https://www.douyin.com/chat")
DOUYIN_SEARCH_URL = os.getenv("DOUYIN_SEARCH_URL", "https://www.douyin.com/search/{keyword}")
DOUYIN_VIDEO_URL = os.getenv("DOUYIN_VIDEO_URL", "https://www.douyin.com/video/{video_id}")
DOUYIN_USER_URL = os.getenv("DOUYIN_USER_URL", "https://www.douyin.com/user/{sec_uid}")

# Service URLs
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation")
DASHSCOPE_EMBEDDING_URL = os.getenv("DASHSCOPE_EMBEDDING_URL", "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

# Server Config
# Default to loopback for packaged desktop installs to avoid firewall prompts.
SERVER_HOST = _get_env("SERVER_HOST", "HOST", default="127.0.0.1")
SERVER_PORT = int(_get_env("SERVER_PORT", "PORT", default="8023"))

# Browser Config
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
BROWSER_EXECUTABLE_PATH = os.getenv("BROWSER_EXECUTABLE_PATH", "").strip()
CLOAKBROWSER_BINARY_PATH = _get_env("CLOAKBROWSER_BINARY_PATH", "BROWSER_EXECUTABLE_PATH", default="")
CLOAKBROWSER_CACHE_DIR = os.getenv("CLOAKBROWSER_CACHE_DIR", "").strip()
CLOAKBROWSER_DOWNLOAD_URL = os.getenv("CLOAKBROWSER_DOWNLOAD_URL", "").strip()
VIEWPORT_SIZE = {"width": 1400, "height": 1080}  # 增大视口高度，确保聊天输入框可见
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Mobile Config (移动端配置)
MOBILE_VIEWPORT_SIZE = {"width": 375, "height": 812}  # iPhone X尺寸
MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
USE_MOBILE_MODE = False  # 是否使用移动端模式（桌面端模式更适合私信操作）


# Timeout Config (milliseconds, Playwright API uses ms)
DEFAULT_TIMEOUT = 60000  # 60s
LOGIN_TIMEOUT = 300000  # 5 minutes for manual login
PAGE_LOAD_TIMEOUT = 120000  # 120s

# Interaction Config
MIN_SLEEP = 3.0
MAX_SLEEP = 5.0
SCROLL_COUNT = 5  # How many times to scroll down to load more videos

# Selectors (To be updated as Douyin changes)
SELECTORS = {
    "login_success_indicator": "#root",
    "search_input": "input[data-e2e='search-input']",
    "video_card": "div[data-e2e='search-card-video']",
    "comment_list": "div[data-e2e='comment-list']",  # 评论列表容器
    "comment_item": "div[data-e2e='comment-item']",  # 单条评论
    "comment_input": "div[data-e2e='comment-input']", # 评论输入框
    "user_nickname": ".user-info .nickname",
    "msg_input": "textarea",
    "close_login_modal": ".dy-account-close", # 登录弹窗关闭按钮
}

# DB Config
DB_PATH = get_customers_db_path()

# Video crawler defaults
CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT = os.getenv("CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT", "true").lower() == "true"
CRAWLER_QUEUE_PRIORITY_DEFAULT = os.getenv("CRAWLER_QUEUE_PRIORITY_DEFAULT", "latest_unprocessed").strip() or "latest_unprocessed"
CRAWLER_WORKER_THREADS_DEFAULT = max(1, int(os.getenv("CRAWLER_WORKER_THREADS_DEFAULT", "1")))
CRAWLER_RESUME_STALE_MINUTES = max(1, int(os.getenv("CRAWLER_RESUME_STALE_MINUTES", "30")))

# TTL Config (seconds)
SESSION_CACHE_TTL = int(os.getenv("SESSION_CACHE_TTL", "1800"))
SENT_MESSAGE_TTL = int(os.getenv("SENT_MESSAGE_TTL", "1800"))
SENT_BY_US_TTL = int(os.getenv("SENT_BY_US_TTL", "1800"))
PROCESSED_MESSAGE_TTL = int(os.getenv("PROCESSED_MESSAGE_TTL", "3600"))
ECHO_DETECTION_WINDOW = int(os.getenv("ECHO_DETECTION_WINDOW", "3600"))
RECENT_SENT_CACHE_TTL = int(os.getenv("RECENT_SENT_CACHE_TTL", "3600"))
SKIPPED_INHERENT_TTL = int(os.getenv("SKIPPED_INHERENT_TTL", "3600"))
SKIPPED_ECHO_TTL = int(os.getenv("SKIPPED_ECHO_TTL", "1800"))
SKIPPED_TRANSIENT_TTL = int(os.getenv("SKIPPED_TRANSIENT_TTL", "120"))
SKIPPED_DEFAULT_TTL = int(os.getenv("SKIPPED_DEFAULT_TTL", "60"))
API_INTERCEPT_INBOUND_WINDOW_SECONDS = max(0, int(os.getenv("API_INTERCEPT_INBOUND_WINDOW_SECONDS", "300")))

USE_NETWORK_DETECTION = os.getenv("USE_NETWORK_DETECTION", "true").lower() == "true"
NETWORK_DETECTION_DEDUP_WINDOW_SECONDS = max(60, int(os.getenv("NETWORK_DETECTION_DEDUP_WINDOW_SECONDS", "600")))

# 阶段4：灰度配置 - 网络拦截流量比例 (0.0-1.0)
NETWORK_DETECTION_TRAFFIC_RATIO = float(os.getenv("NETWORK_DETECTION_TRAFFIC_RATIO", "1.0"))
NETWORK_DETECTION_COMPARE_LOG_ENABLED = os.getenv("NETWORK_DETECTION_COMPARE_LOG_ENABLED", "true").lower() == "true"

# CRAG / reply fallback config
CRAG_MIN_RELEVANCE = float(os.getenv("CRAG_MIN_RELEVANCE", "0.22"))
# 修复 N4：降低 CRAG_PASS_THRESHOLD 从 0.52 到 0.45，避免语义相似但表述不同的查询被误判为 retry
CRAG_PASS_THRESHOLD = float(os.getenv("CRAG_PASS_THRESHOLD", "0.45"))
CRAG_RETRY_THRESHOLD = float(os.getenv("CRAG_RETRY_THRESHOLD", "0.36"))

# Reranker 模型配置（P0-2: 从硬编码迁移到配置化，默认升级为 bge-reranker-v2-m3）
# 留空时使用此默认路径；模型不存在时自动降级到 _simple_rerank 关键词匹配
RERANKER_MODEL_PATH = os.getenv("RERANKER_MODEL_PATH", "") or "models/embedding/reranker/bge-reranker-v2-m3"
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() == "true"
RERANKER_MAX_LENGTH = int(os.getenv("RERANKER_MAX_LENGTH", "512"))
# 修复 R12：本地模型输出较慢，CRAG 改写超时从 5s 提升到 15s
CRAG_REWRITE_TIMEOUT_SECONDS = float(os.getenv("CRAG_REWRITE_TIMEOUT_SECONDS", "15"))
# 修复 R12：本地模型输出较慢，主链生成超时从 12s 提升到 30s
_llm_timeout_default = os.getenv("OLLAMA_TIMEOUT_SECONDS", "30")
REPLY_LLM_TIMEOUT_SECONDS = float(os.getenv("REPLY_LLM_TIMEOUT_SECONDS", _llm_timeout_default))
LLM_ONLY_FALLBACK_TIMEOUT_SECONDS = float(
    os.getenv("LLM_ONLY_FALLBACK_TIMEOUT_SECONDS", str(REPLY_LLM_TIMEOUT_SECONDS))
)
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
# [PERF-INST:reply-latency] num_predict 从 160 降到 96：
# - 实测 90% 回复 < 100 字符，"您好，我是智揽..." 类短句
# - 减小 num_predict 显著降低 CPU 推理时间（47s→预计 25s）
# - 用户可调高，需更长回复时设 OLLAMA_NUM_PREDICT=160
# 修复：96 token 约 48-96 汉字，prompt 要求"50-150字"会导致截断，提升到 256
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "256"))
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
OLLAMA_TOP_P = float(os.getenv("OLLAMA_TOP_P", "0.8"))
OLLAMA_TOP_K = int(os.getenv("OLLAMA_TOP_K", "20"))
OLLAMA_ENABLE_THINKING = os.getenv("OLLAMA_ENABLE_THINKING", "false").lower() == "true"
OLLAMA_THINKING_DEPTH = int(os.getenv("OLLAMA_THINKING_DEPTH", "0"))
ENABLE_LLM_ONLY_FALLBACK = os.getenv("ENABLE_LLM_ONLY_FALLBACK", "true").lower() == "true"
ENABLE_RETRY_DIRECT_ANSWER = os.getenv("ENABLE_RETRY_DIRECT_ANSWER", "true").lower() == "true"
