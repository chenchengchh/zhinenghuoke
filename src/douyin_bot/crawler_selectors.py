"""
抖音爬虫选择器常量及选择器工具方法。

从 Crawler 类中提取的 CSS 选择器常量与布局感知的选择器构建逻辑，
供 Crawler 及各 Mixin 复用。
"""
from typing import Optional


# ---------------------------------------------------------------------------
# CSS 选择器常量
# ---------------------------------------------------------------------------

SEARCH_INPUT_SELECTORS = [
    'input[placeholder*="搜索"]',
    'input[type="search"]',
    'input[data-e2e*="search"]',
    'input[class*="search"]',
    '[role="searchbox"] input',
    '[role="textbox"][placeholder*="搜索"]',
    '[contenteditable="true"][data-e2e*="search"]',
    '[contenteditable="true"][placeholder*="搜索"]',
]

SEARCH_BUTTON_SELECTORS = [
    'button:has-text("搜索")',
    '[data-e2e="search-button"]',
    'button[class*="search"]',
    '[role="button"][aria-label*="搜索"]',
]

SEARCH_RESULT_WAIT_SELECTORS = [
    '[data-e2e="search-common-video"]',
    '[data-e2e="search-item"]',
    '[class*="search-result"]',
    '[class*="searchResult"]',
    '[class*="video-item"]',
]

SEARCH_GENERAL_TAB_SELECTORS = [
    '[role="tab"]:has-text("综合")',
    '[role="tab"]:has-text("全部")',
    'a:has-text("综合")',
    'a:has-text("全部")',
    '[class*="tab"]:has-text("综合")',
    '[class*="tab"]:has-text("全部")',
]

SEARCH_LAYOUT_MULTI_COLUMN_TEXTS = ("多列", "双列")
SEARCH_LAYOUT_SINGLE_COLUMN_TEXTS = ("单列",)

SEARCH_LAYOUT_CONTROL_SELECTORS = [
    'button:has-text("多列")',
    '[role="button"]:has-text("多列")',
    '[role="tab"]:has-text("多列")',
    'div:has-text("多列")',
    'span:has-text("多列")',
]

LOGIN_MODAL_CLOSE_SELECTORS = [
    '[data-e2e="login-close"]',
    '[data-e2e="modal-close-inner-button"]',
    '[class*="close"]:has(svg)',
    'button[aria-label*="关闭"]',
    'button:has-text("关闭")',
]

RECOMMENDED_VIDEO_CLOSE_SELECTORS = [
    '[data-e2e="feed-close"]',
    '[data-e2e="detail-close"]',
    '[data-e2e="video-detail-close"]',
    '[data-e2e="close-detail"]',
    '.xgplayer-close',
    '.video-detail-close',
    'div[class*="close"]',
    'svg[class*="close"]',
]

RECOMMENDED_VIDEO_SURFACE_SELECTORS = [
    '[data-e2e="feed-active-video"]',
    '[data-e2e="detail-player"]',
    '[data-e2e="video-player"]',
    '[data-e2e="recommend-video"]',
    '[data-e2e="video-detail"]',
    '[class*="video-detail"]',
    '[class*="detail-player"]',
    '[class*="recommend-video"]',
    '[class*="player-container"]',
    '[class*="xgplayer"]',
]

COMMENT_SURFACE_SELECTORS = [
    '[data-e2e="comment-list"]',
    '[data-e2e="comment-list-container"]',
    '[data-e2e="comment-panel"]',
    '[data-e2e="note-comment-list"]',
    '[class*="comment-list"]',
    '[class*="CommentList"]',
    '[class*="comment-panel"]',
    '[class*="commentContent"]',
    '[class*="comment-drawer"]',
    '[class*="CommentDrawer"]',
    '[class*="commentContainer"]',
    '[class*="CommentContainer"]',
    '[class*="commentWrap"]',
    '[class*="CommentWrap"]',
    '[class*="commentScroll"]',
    '[class*="CommentScroll"]',
    '.comment-main',
]

COMMENT_ITEM_SELECTORS = [
    '[data-e2e="comment-item"]',
    '[class*="comment-item"]',
    '[class*="CommentItem"]',
]

COMMENT_OPEN_TRIGGER_SELECTORS = [
    "[data-e2e='comment-icon']",
    "[data-e2e='feed-comment-icon']",
    "[data-e2e='browse-comment']",
    "[data-e2e='video-comment-icon']",
    "[data-e2e='comment-count']",
    "button[aria-label*='评论']",
    "[role='button'][aria-label*='评论']",
    "[class*='comment-icon']",
    "[class*='CommentIcon']",
    "[class*='commentIcon']",
    "[class*='comment-count']",
    "[class*='commentCount']",
]


# ---------------------------------------------------------------------------
# 选择器工具方法
# ---------------------------------------------------------------------------

def _dedupe_selector_list(selectors: list[str]) -> list[str]:
    result = []
    seen = set()
    for selector in selectors or []:
        normalized = str(selector or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _get_comment_surface_selectors_for_layout(detail_kind: str) -> list[str]:
    note_priority = [
        '[data-e2e="note-comment-list"]',
        '[class*="commentContainer"]',
        '[class*="CommentContainer"]',
        '[class*="commentWrap"]',
        '[class*="CommentWrap"]',
        '[class*="commentScroll"]',
        '[class*="CommentScroll"]',
    ]
    video_priority = [
        '[data-e2e="comment-panel"]',
        '[data-e2e="comment-list"]',
        '[data-e2e="comment-list-container"]',
        '[class*="comment-drawer"]',
        '[class*="CommentDrawer"]',
        '[class*="comment-panel"]',
        '[class*="commentContent"]',
        '.comment-main',
    ]
    if detail_kind == "note":
        return _dedupe_selector_list(note_priority + list(COMMENT_SURFACE_SELECTORS))
    if detail_kind == "video":
        return _dedupe_selector_list(video_priority + list(COMMENT_SURFACE_SELECTORS))
    return list(COMMENT_SURFACE_SELECTORS)


def _get_comment_item_selectors_for_layout(detail_kind: str) -> list[str]:
    note_priority = [
        '[data-e2e="note-comment-list"] [data-e2e="comment-item"]',
        '[data-e2e="note-comment-list"] li[class*="comment"]',
        '[data-e2e="note-comment-list"] article[class*="comment"]',
        '[class*="commentContainer"] [data-e2e="comment-item"]',
        '[class*="CommentContainer"] [data-e2e="comment-item"]',
        '[data-e2e="comment-item"]',
        '[class*="comment-item"]',
        '[class*="CommentItem"]',
        'li[class*="comment"]',
        'article[class*="comment"]',
    ]
    video_priority = [
        '[data-e2e="comment-item"]',
        '[class*="comment-item"]',
        '[class*="CommentItem"]',
        'li[class*="comment"]',
    ]
    if detail_kind == "note":
        return _dedupe_selector_list(note_priority + list(COMMENT_ITEM_SELECTORS))
    if detail_kind == "video":
        return _dedupe_selector_list(video_priority + list(COMMENT_ITEM_SELECTORS))
    return list(COMMENT_ITEM_SELECTORS)


def _get_comment_composer_container_selectors(detail_kind: str) -> list[str]:
    note_priority = [
        '[data-e2e="comment-input"]',
        '.comment-input-inner-container',
        '.comment-input-inner',
        '.comment-input-area',
        '[class*="comment-input"]',
        '[class*="commentInput"]',
        '[class*="CommentInput"]',
    ]
    video_priority = [
        '#comment-input-container',
        '.kBeyEVwN#comment-input-container',
        '[data-e2e="comment-input"]',
        '.comment-input-inner-container',
        '.comment-input-inner',
        '.comment-input-area',
        '[class*="comment-input"]',
        '[class*="commentInput"]',
        '[class*="CommentInput"]',
    ]
    if detail_kind == "note":
        return _dedupe_selector_list(note_priority + video_priority)
    if detail_kind == "video":
        return _dedupe_selector_list(video_priority + note_priority)
    return _dedupe_selector_list(video_priority + note_priority)
