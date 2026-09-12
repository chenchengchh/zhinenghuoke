"""
Web 搜索服务 (WebSearchService)

P1-4: CRAG no_answer 路径的 Web 搜索兜底。
默认使用 DuckDuckGo（免费无 API key），支持多引擎扩展。
通过 schema metadata.retrieval_policy.web_search_fallback 控制启用。
"""

import logging
import re
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


class WebSearchService:
    """Web 搜索服务，支持 DuckDuckGo 等搜索引擎。

    设计原则：
    1. 默认关闭，schema 显式启用时生效
    2. 全链路降级：任何异常都返回空列表，不阻断主链
    3. 超时保护：默认 5 秒，避免阻塞回复
    4. 结果标准化：[{title, url, snippet, source}]
    """

    def __init__(
        self,
        engine: str = "duckduckgo",
        max_results: int = 3,
        timeout_seconds: int = 5,
    ):
        self.engine = str(engine or "duckduckgo").strip().lower()
        self.max_results = max(1, int(max_results or 3))
        self.timeout_seconds = max(1, int(timeout_seconds or 5))

    def search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """执行 Web 搜索，返回标准化结果列表。

        Args:
            query: 搜索查询词
            top_k: 覆盖 max_results 的结果数上限

        Returns:
            List[Dict]: [{title, url, snippet, source}]，失败返回空列表
        """
        query = str(query or "").strip()
        if not query:
            return []

        effective_top_k = min(top_k or self.max_results, self.max_results)

        try:
            if self.engine == "duckduckgo":
                results = self._search_duckduckgo(query, effective_top_k)
            else:
                logger.warning(f"不支持的搜索引擎: {self.engine}，跳过 Web 搜索")
                return []
            logger.info(f"Web 搜索完成: query='{query[:30]}', results={len(results)}")
            return results
        except Exception as exc:
            logger.warning(f"Web 搜索失败（降级为空结果）: {exc}")
            return []

    def _search_duckduckgo(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """DuckDuckGo 搜索：优先使用官方库，回退到 HTML 抓取。"""
        # 路径 1：优先使用 duckduckgo_search / ddgs 库（如果已安装）
        results = self._search_ddgs_library(query, top_k)
        if results:
            return results

        # 路径 2：回退到 DuckDuckGo HTML 抓取
        return self._search_ddg_html(query, top_k)

    def _search_ddgs_library(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """使用 duckduckgo_search / ddgs 库搜索（需 pip install）。"""
        try:
            # 新版包名 ddgs，旧版 duckduckgo_search
            ddgs = None
            try:
                from ddgs import DDGS
                ddgs = DDGS()
            except ImportError:
                try:
                    from duckduckgo_search import DDGS
                    ddgs = DDGS()
                except ImportError:
                    return []

            results = []
            for item in ddgs.text(query, max_results=top_k):
                results.append({
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("href") or item.get("url") or ""),
                    "snippet": str(item.get("body") or item.get("snippet") or ""),
                    "source": "duckduckgo_library",
                })
            return results
        except Exception as exc:
            logger.debug(f"duckduckgo_search 库不可用或调用失败: {exc}")
            return []

    def _search_ddg_html(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """DuckDuckGo HTML 抓取兜底（无需额外依赖）。"""
        try:
            import requests
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            resp = requests.get(url, headers=headers, timeout=self.timeout_seconds)
            resp.raise_for_status()
            html = resp.text

            results = []
            # DuckDuckGo HTML 结果块
            blocks = re.findall(
                r'<a[^>]+class="result__a"[^>]*>(.*?)</a>.*?'
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>.*?'
                r'<a[^>]+class="result__url"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )
            # 备用正则：更宽松的匹配
            if not blocks:
                blocks = re.findall(
                    r'<a[^>]+rel="nofollow"[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                    r'class="result__snippet">(.*?)</a>',
                    html,
                    re.DOTALL,
                )
                for url_match, title_match, snippet_match in blocks[:top_k]:
                    clean_title = self._strip_html_tags(title_match).strip()
                    clean_snippet = self._strip_html_tags(snippet_match).strip()
                    if clean_title:
                        results.append({
                            "title": clean_title,
                            "url": url_match,
                            "snippet": clean_snippet,
                            "source": "duckduckgo_html",
                        })
            else:
                for title_match, snippet_match, url_match in blocks[:top_k]:
                    clean_title = self._strip_html_tags(title_match).strip()
                    clean_snippet = self._strip_html_tags(snippet_match).strip()
                    clean_url = self._strip_html_tags(url_match).strip()
                    if clean_title:
                        results.append({
                            "title": clean_title,
                            "url": clean_url,
                            "snippet": clean_snippet,
                            "source": "duckduckgo_html",
                        })
            return results
        except Exception as exc:
            logger.debug(f"DuckDuckGo HTML 抓取失败: {exc}")
            return []

    @staticmethod
    def _strip_html_tags(text: str) -> str:
        """移除 HTML 标签，保留纯文本。"""
        return re.sub(r"<[^>]+>", "", str(text or ""))

    def format_results_as_context(
        self,
        results: List[Dict[str, Any]],
        query: str = "",
    ) -> List[Dict[str, Any]]:
        """将 Web 搜索结果格式化为 RAG contexts 格式。

        Args:
            results: search() 返回的结果列表
            query: 原始查询（用于相关性标注）

        Returns:
            List[Dict]: 符合 retrieved_contexts 格式的弱证据列表
        """
        contexts = []
        for idx, item in enumerate(results):
            title = str(item.get("title") or "")
            snippet = str(item.get("snippet") or "")
            url = str(item.get("url") or "")
            content = f"{title}。{snippet}".strip("。")
            if not content:
                continue
            contexts.append({
                "question": query,
                "answer": content,
                "source": "web_search_fallback",
                "url": url,
                "relevance_score": 0.15,  # 弱证据标记
                "category": "web_search",
                "metadata": {
                    "source": item.get("source", "web_search"),
                    "rank": idx,
                    "warning": "网络搜索结果，仅供参考",
                },
            })
        return contexts


# ---------------------------------------------------------------------------
# 单例工厂
# ---------------------------------------------------------------------------

_web_search_service: Optional[WebSearchService] = None


def get_web_search_service(
    engine: str = "duckduckgo",
    max_results: int = 3,
    timeout_seconds: int = 5,
) -> WebSearchService:
    """获取 Web 搜索服务单例。"""
    global _web_search_service
    if _web_search_service is None:
        _web_search_service = WebSearchService(
            engine=engine,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
        )
    return _web_search_service


def reset_web_search_service() -> None:
    """重置单例（测试用）。"""
    global _web_search_service
    _web_search_service = None
