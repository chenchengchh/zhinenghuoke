"""
抖音DOM选择器与共享脚本

集中管理DOM选择器、消息过滤规则和JavaScript脚本，
消除MessageMonitor与RPAEngine之间的代码重复。
"""

CONVERSATION_ITEM_SELECTORS = [
    '[data-e2e="conversation-item"]',
    '.chat-list-item',
    '[class*="conversationItem"]',
    '[class*="chat-item"]'
]

NAME_ELEMENT_SELECTORS = [
    '.conversationConversationItemtitle',
    '[class*="title"]',
    '[class*="name"]'
]

UNREAD_ELEMENT_SELECTORS = [
    '[class*="commonStreak"]',
    '[class*="unread"]',
    '[class*="badge"]',
    '[class*="dot"]',
    '[class*="msgCount"]',
    '[class*="count"]'
]

SELF_MESSAGE_SELECTORS = [
    '[class*="self-message"]',
    '[class*="msg-self"]',
    '[class*="outbound"]'
]

SENDER_ELEMENT_SELECTORS = [
    '[class*="sender"]'
]

INPUT_BOX_SELECTORS = [
    '[data-e2e="msg-input"] [contenteditable="true"]',
    '[data-e2e="msg-input"] [role="textbox"]',
    '[data-e2e="msg-input"] textarea',
    '[class*="chat-input"] [contenteditable="true"]',
    '[class*="chat-input"] [role="textbox"]',
    '[class*="chat-input"] textarea',
    '[contenteditable="true"][role="textbox"]',
    '[contenteditable="true"][class*="editor"]',
    '[contenteditable="true"][class*="input"]',
    '[contenteditable="true"][class*="chat"]',
    '[contenteditable="true"][class*="message"]',
    '[class*="messageEditor"]',
    '[class*="editor-kit"]',
    '[class*="chat-input"]',
    '[class*="msgInput"]',
    '[class*="inputArea"]',
    'textarea[class*="chat"]',
    'textarea[class*="message"]',
    'div[class*="public-DraftEditor-content"]',
    'textarea',
    'div[contenteditable="true"]',
]

CONVERSATION_WAIT_SELECTORS = ', '.join(CONVERSATION_ITEM_SELECTORS[:2])

INPUT_WAIT_SELECTORS = ', '.join([
    'div[contenteditable="true"]',
    '[contenteditable]',
    'textarea'
])

INVALID_PATTERNS = [
    '已撤回', '正在输入', '正在加载',
    '滑动的', '上拉', '下拉', '系统消息', '系统通知',
    '[图片]', '[语音]', '[视频]', '[表情]', '[文件]', '[链接]',
    '[红包]', '[位置]', '[名片]', '[小程序]', '[商品]'
]

STATUS_INDICATOR_PATTERNS = [
    '小时前在线', '分钟前在线', '刚刚在线', '天前在线',
    '小时前离线', '分钟前离线', '天前离线',
]

BOT_REPLY_PATTERNS = [
    'Thinking Process',
    'Analyze the Request',
    'Professional Sales Consultant',
    'General Service Sales',
    '对公转账需要：公司名称',
    '方便的话也可以留个接收资料的联系方式',
    '更完整的资料、详细说明',
    '继续为您处理',
]

TIME_PATTERNS = [
    r'^\d{1,2}:\d{2}$',
    r'^\d{1,2}:\d{2}:\d{2}$',
    r'^昨天$',
    r'^\d+分钟前$',
    r'^\d+小时前$',
    r'^\d+天前$',
    r'^\d{4}[/\-]\d{1,2}[/\-]\d{1,2}$',
    r'^\d+$',
]

MIN_MESSAGE_LENGTH = 2
MAX_MESSAGE_LENGTH = 2000

# ==================== 私信发送相关选择器 ====================

# 私信按钮选择器（按置信度排序）
PRIVATE_MESSAGE_BUTTON_SELECTORS = [
    "[data-e2e='send-message-btn']",
    "button[data-e2e='send-message-btn']",
    "button:has-text('私信')",
    "button:has-text('发消息')",
    "button:has-text('聊天')",
    ".semi-button-content:has-text('私信')",
    "button:has(.semi-button-content:has-text('私信'))",
    "[class*='send-message']",
    "[class*='message-btn']",
    "div[data-e2e='user-info'] button",
    "div[class*='user-info'] button",
    "div[class*='im-icon']",
    "button:has(svg)",
    "[class*='chat-btn']",
]

# 更多操作菜单选择器
MORE_ACTIONS_SELECTORS = [
    "div[data-e2e='more-actions']",
    ".more-actions",
]

# 输入框选择器
MESSAGE_INPUT_SELECTORS = [
    '[data-e2e="msg-input"] [contenteditable="true"]',
    '[data-e2e="msg-input"] [role="textbox"]',
    "textarea[data-e2e='chat-input']",
    "textarea.chat-input",
    "[class*='chat-input'] [contenteditable='true']",
    "div[class*='public-DraftEditor-content']",
    ".chat-input-container textarea",
    "div[contenteditable='true']",
    "div[role='textbox']",
]

# 关闭聊天按钮选择器
CLOSE_CHAT_SELECTORS = [
    "div[data-e2e='close-chat']",
]

# 发送失败图标选择器
SEND_FAIL_INDICATOR_SELECTORS = [
    ".msg-resend-icon",
    "svg[class*='fail']",
]

# 登录弹窗选择器
LOGIN_POPUP_SELECTORS = [
    ".dy-account-close",
    "text='登录后查看'",
]

# 用户信息选择器
USER_INFO_SELECTORS = [
    "div[data-e2e='user-info']",
]


def get_conversations_js() -> str:
    """
    获取会话列表的JavaScript代码

    返回包含以下字段的会话对象数组：
    - customer_name: 客户名称
    - last_message_content: 最后消息内容
    - last_message_time: 最后消息时间
    - direction: 消息方向 (inbound/outbound)
    - unread_count: 未读消息数
    - has_unread: 是否有未读消息
    """
    return """
    () => {
        const conversations = [];
        const selectors = [
            '[data-e2e="conversation-item"]',
            '.chat-list-item',
            '[class*="conversationItem"]',
            '[class*="chat-item"]'
        ];

        let items = [];
        for (const selector of selectors) {
            const found = document.querySelectorAll(selector);
            if (found.length > 0) {
                items = found;
                break;
            }
        }

        for (const item of items) {
            try {
                const nameEl = item.querySelector('.conversationConversationItemtitle') ||
                               item.querySelector('[class*="title"]') ||
                               item.querySelector('[class*="name"]');
                const customerName = nameEl ? nameEl.textContent.trim() : '';

                if (!customerName || customerName.length > 50) continue;

                const fullText = item.innerText || '';
                const lines = fullText.split('\\n').filter(line => line.trim());
                let lastMessage = '';

                for (const line of lines) {
                    const trimmed = line.trim();
                    if (trimmed === customerName) continue;
                    if (/^\\d{1,2}:\\d{2}$/.test(trimmed)) continue;
                    if (/^(昨天|刚刚|\\d+分钟前|\\d+小时前|\\d+天前|前天)$/.test(trimmed)) continue;
                    if (/^\\d+$/.test(trimmed) && trimmed.length < 3) continue;
                    if (trimmed.length > 0) {
                        lastMessage = trimmed;
                        break;
                    }
                }

                let direction = 'inbound';
                const selfMsgEl = item.querySelector('[class*="self-message"], [class*="msg-self"], [class*="outbound"]');
                if (selfMsgEl) {
                    direction = 'outbound';
                }
                const senderEl = item.querySelector('[class*="sender"]');
                if (senderEl && (senderEl.textContent.trim() === '我' ||
                    senderEl.textContent.trim() === '我发送的')) {
                    direction = 'outbound';
                }
                if (lastMessage.startsWith('我:') || lastMessage.startsWith('我：')) {
                    direction = 'outbound';
                }
                if (direction === 'inbound' && lastMessage.startsWith('[自动回复]')) {
                    direction = 'outbound';
                }

                let unreadCount = 0;
                const unreadSelectors = [
                    '[class*="commonStreak"]',
                    '[class*="unread"]',
                    '[class*="badge"]',
                    '[class*="dot"]',
                    '[class*="msgCount"]',
                    '[class*="count"]'
                ];
                for (const sel of unreadSelectors) {
                    const unreadEl = item.querySelector(sel);
                    if (unreadEl) {
                        const text = unreadEl.textContent.trim();
                        const num = parseInt(text);
                        if (!isNaN(num) && num > 0) {
                            unreadCount = num;
                            break;
                        } else if (unreadEl.offsetWidth > 0 && text.length > 0) {
                            unreadCount = 1;
                            break;
                        }
                    }
                }

                const hasUnread = item.querySelector('[class*="unread"]') !== null ||
                                  unreadCount > 0;

                conversations.push({
                    customer_name: customerName,
                    last_message_content: lastMessage,
                    last_message_time: item.querySelector('.conversationConversationItemtime')?.textContent?.trim() || '',
                    direction: direction,
                    unread_count: unreadCount,
                    has_unread: hasUnread
                });
            } catch (e) {}
        }
        return conversations;
    }
    """


def get_click_conversation_js() -> str:
    """
    点击指定客户会话的JavaScript代码

    使用参数传递避免JS注入，支持精确匹配和模糊匹配
    """
    return """
    (targetNameRaw) => {
        const isMaskedNumericMatch = (candidate, target) => {
            const candidateDigits = (candidate || '').replace(/\\D/g, '');
            const targetDigits = (target || '').replace(/\\D/g, '');
            if (!candidateDigits || !targetDigits) return false;
            if (candidateDigits === targetDigits) return true;
            if (Math.min(candidateDigits.length, targetDigits.length) < 7) return false;
            const prefixLen = Math.min(4, candidateDigits.length, targetDigits.length);
            const suffixLen = Math.min(4, candidateDigits.length, targetDigits.length);
            return candidateDigits.slice(0, prefixLen) === targetDigits.slice(0, prefixLen) &&
                   candidateDigits.slice(-suffixLen) === targetDigits.slice(-suffixLen);
        };
        const targetName = targetNameRaw.replace(/^@/, '');
        const selectors = [
            '[data-e2e="conversation-item"]',
            '.chat-list-item',
            '[class*="conversationItem"]',
            '[class*="chat-item"]'
        ];

        let items = [];
        for (const selector of selectors) {
            const found = document.querySelectorAll(selector);
            if (found.length > 0) {
                items = found;
                break;
            }
        }

        let exactMatch = null;
        let fuzzyMatch = null;
        for (const item of items) {
            const nameEl = item.querySelector('.conversationConversationItemtitle') ||
                           item.querySelector('[class*="title"]') ||
                           item.querySelector('[class*="name"]');
            const elName = nameEl ? nameEl.textContent.trim().replace(/^@/, '') : '';
            if (elName === targetName) {
                exactMatch = { item, elName };
                break;
            }
            if (!fuzzyMatch && Math.abs(elName.length - targetName.length) <= 2 &&
                (elName.includes(targetName) || targetName.includes(elName) || isMaskedNumericMatch(elName, targetName))) {
                fuzzyMatch = { item, elName };
            }
        }

        const match = exactMatch || fuzzyMatch;
        if (match) {
            match.item.scrollIntoView({ behavior: 'smooth', block: 'center' });

            const rect = match.item.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;

            ['mousedown', 'mouseup', 'click'].forEach(eventType => {
                const event = new MouseEvent(eventType, {
                    bubbles: true, cancelable: true, view: window,
                    clientX: centerX, clientY: centerY
                });
                match.item.dispatchEvent(event);
            });

            return { success: true, matchedName: match.elName, matchType: exactMatch ? 'exact' : 'fuzzy' };
        }
        return { success: false };
    }
    """
