"""
小红书DOM选择器与共享脚本

基于实际页面结构抓取，集中管理DOM选择器、消息过滤规则和JavaScript脚本。
与 douyin_bot/dom_selectors.py 对应，为小红书平台提供相同能力。

页面结构关键发现：
- 技术栈: Vue.js
- 笔记卡片: section.note-item
- 搜索输入: input.search-input
- 频道Tab: div.channel / span.channel
- 侧边栏: div.side-bar
- 频道列表: div.channel-list
- 作者信息: div.author-wrapper > span / div.author-avatar
- 点赞: div.like-wrapper / span.like-active
- 登录弹窗: div.login-modal / div.login-container
- UI组件库: reds-* 前缀 (如 reds-button-new, reds-alert, reds-mask)
"""

# ==================== URL 常量 ====================
XHS_HOME_URL = "https://www.xiaohongshu.com"
XHS_EXPLORE_URL = "https://www.xiaohongshu.com/explore"
XHS_SEARCH_URL = "https://www.xiaohongshu.com/search_result"
XHS_MESSAGE_URL = "https://www.xiaohongshu.com/message"
XHS_USER_PROFILE_URL = "https://www.xiaohongshu.com/user/profile"

# ==================== 导航/头部 ====================
HEADER_SELECTORS = [
    "div.header",
    "div.header-container",
    "img.header-logo",
]

# ==================== 搜索相关 ====================
SEARCH_INPUT_SELECTORS = [
    'input.search-input',
    'input[placeholder*="搜索"]',
    'input[placeholder*="探索"]',
]

SEARCH_BUTTON_SELECTORS = [
    'div.input-button',
    'div.search-icon',
]

# ==================== 频道/分类Tab ====================
CHANNEL_TAB_SELECTORS = [
    "div.channel",
    "span.channel",
    "div.channel-container div.channel",
    "div.scroll-container.channel-scroll-container div.channel",
]

ACTIVE_CHANNEL_SELECTOR = "div.active.channel"

CHANNEL_LIST_SELECTOR = "div.channel-list"

# ==================== 笔记卡片 ====================
NOTE_ITEM_SELECTORS = [
    "section.note-item",
    "div.feeds-container section",
]

NOTE_LINK_PATTERN = "/explore/"

NOTE_TITLE_SELECTORS = [
    "span.title",
    "div.note-content span",
]

NOTE_AUTHOR_SELECTORS = [
    "div.author-wrapper span",
    "span.name",
    "div.author-avatar",
]

NOTE_LIKE_SELECTORS = [
    "div.like-wrapper span",
    "span.like-active",
    "div.like-wrapper",
]

NOTE_COVER_SELECTORS = [
    "img[data-xhs-img]",
]

# ==================== 笔记详情页 ====================
NOTE_DETAIL_AUTHOR_SELECTORS = [
    "div.author-wrapper",
    "div.author-avatar",
    "span.user-name",
]

NOTE_DETAIL_CONTENT_SELECTORS = [
    "div.note-text",
    "div.desc",
    "span.note-text",
]

NOTE_DETAIL_COMMENT_SELECTORS = [
    "div.comment-item",
    "div.comment-container div",
]

# ==================== 消息/私信相关 ====================
# 注意: 小红书网页版消息页需要登录后才能访问
# 消息页URL: https://www.xiaohongshu.com/message
# 登录后消息页的具体DOM结构需要在登录状态下进一步探索

CONVERSATION_ITEM_SELECTORS = [
    '[class*="chat-item"]',
    '[class*="session-item"]',
    '[class*="conversation-item"]',
    '[class*="contact-item"]',
]

MESSAGE_INPUT_SELECTORS = [
    'textarea',
    'div[contenteditable="true"]',
    '[class*="chat-input"]',
    '[class*="message-input"]',
    '[class*="editor"]',
]

PRIVATE_MESSAGE_BUTTON_SELECTORS = [
    'button:has-text("私信")',
    '[class*="message-btn"]',
    '[class*="chat-btn"]',
    '[class*="send-message"]',
]

# ==================== 用户主页 ====================
USER_PROFILE_SELECTORS = [
    "div.user-name",
    "span.user-name",
    "div.user-desc",
]

USER_STATS_SELECTORS = [
    "div.user-info span",
    "[class*='fan']",
    "[class*='follow']",
    "[class*='count']",
]

# ==================== 登录相关 ====================
LOGIN_MODAL_SELECTORS = [
    "div.login-modal",
    "div.login-container",
    "div.reds-modal.login-modal",
]

LOGIN_BUTTON_SELECTORS = [
    "div.side-bar-component.login-btn",
    "button:has-text('登录')",
    "div.bottom-channel:has-text('登录')",
]

LOGIN_QRCODE_SELECTORS = [
    "img.qrcode-img",
    "div.qrcode-img",
]

# ==================== 侧边栏 ====================
SIDEBAR_SELECTORS = [
    "div.side-bar",
    "div.side-bar-component",
]

SIDEBAR_NAV_ITEMS = {
    "discover": "发现",
    "live": "直播",
    "publish": "发布",
    "notification": "通知",
    "me": "我",
}

# ==================== UI组件 (reds-* 前缀) ====================
REDS_BUTTON_SELECTOR = "div.reds-button-new"
REDS_ALERT_SELECTOR = "div.reds-alert"
REDS_MASK_SELECTOR = "div.reds-mask"
REDS_MODAL_SELECTOR = "div.reds-modal"

# ==================== 数据属性 ====================
# 小红书自定义data属性
DATA_ATTRS = {
    "image": "data-xhs-img",
    "logged": "data-logged",
    "width": "data-width",
    "height": "data-height",
    "index": "data-index",
}

# ==================== 消息过滤 ====================
INVALID_PATTERNS = [
    '已撤回', '正在输入', '正在加载',
    '系统消息', '系统通知',
    '[图片]', '[语音]', '[视频]', '[表情]', '[文件]', '[链接]',
]

MIN_MESSAGE_LENGTH = 2
MAX_MESSAGE_LENGTH = 2000


def get_note_items_js() -> str:
    """获取笔记列表的JavaScript代码"""
    return """
    () => {
        const notes = [];
        const items = document.querySelectorAll('section.note-item');
        for (const item of items) {
            try {
                const linkEl = item.querySelector('a[href*="/explore/"]');
                const href = linkEl ? linkEl.href : '';
                const noteId = href ? href.match(/\\/explore\\/([a-f0-9]+)/)?.[1] || '' : '';

                const titleEl = item.querySelector('span.title, div.note-content span');
                const title = titleEl ? titleEl.textContent.trim() : '';

                const authorEl = item.querySelector('div.author-wrapper span, span.name');
                const author = authorEl ? authorEl.textContent.trim() : '';

                const likeEl = item.querySelector('div.like-wrapper span');
                const likes = likeEl ? likeEl.textContent.trim() : '0';

                const imgEl = item.querySelector('img[data-xhs-img]');
                const coverUrl = imgEl ? imgEl.src : '';

                if (title || noteId) {
                    notes.push({
                        note_id: noteId,
                        title: title,
                        author: author,
                        likes: likes,
                        cover_url: coverUrl,
                        url: href,
                    });
                }
            } catch (e) {}
        }
        return notes;
    }
    """


def get_search_results_js() -> str:
    """获取搜索结果的JavaScript代码"""
    return """
    () => {
        const results = [];
        const items = document.querySelectorAll('section.note-item, [class*="note-item"]');
        for (const item of items) {
            try {
                const linkEl = item.querySelector('a[href*="/explore/"]');
                const href = linkEl ? linkEl.href : '';
                const noteId = href ? href.match(/\\/explore\\/([a-f0-9]+)/)?.[1] || '' : '';

                const titleEl = item.querySelector('span.title, [class*="title"]');
                const title = titleEl ? titleEl.textContent.trim() : '';

                const authorEl = item.querySelector('[class*="author"] span, [class*="name"]');
                const author = authorEl ? authorEl.textContent.trim() : '';

                if (title || noteId) {
                    results.push({
                        note_id: noteId,
                        title: title,
                        author: author,
                        url: href,
                    });
                }
            } catch (e) {}
        }
        return results;
    }
    """
