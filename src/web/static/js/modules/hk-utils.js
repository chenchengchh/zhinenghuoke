/**
 * HuoKe 工具函数模块
 *
 * 提供与 main.js 行为完全一致的纯工具函数（format / get / build / escape / sanitize 等），
 * 挂载到 window.HKUtils 命名空间，便于：
 *   1. 在新代码中按需引用（HKUtils.escapeHtml(...)）；
 *   2. 独立编写单元测试；
 *   3. 后续逐步将 main.js 的同名函数替换为对 HKUtils 的引用。
 *
 * 与 main.js 的关系：
 *   - 本模块不依赖 main.js 任何状态或函数；
 *   - main.js 仍保留同名函数定义，行为完全等价，作为兼容性兜底；
 *   - 本文件必须先于 main.js 引入。
 */
(function (global) {
    'use strict';

    // ============== HTML 转义 / 数值清洗 ==============

    /**
     * HTML 转义，避免 XSS
     * @param {string} text
     * @returns {string}
     */
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * 百分比数值清洗，限制在 0~100 整数区间
     * @param {number|string} value
     * @returns {number}
     */
    function sanitizePercent(value) {
        const num = parseFloat(value);
        if (isNaN(num)) return 0;
        return Math.max(0, Math.min(100, Math.round(num)));
    }

    // ============== 时间格式化 ==============

    /**
     * 汇总时间字符串格式化（不限 / 当地时区）
     */
    function formatSummaryTime(value) {
        if (!value) return '不限';
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
    }

    /**
     * 客户评论时间格式化：支持秒/毫秒时间戳、ISO 字符串、Date 等
     */
    function formatCustomerCommentTime(value) {
        if (value === null || value === undefined || value === '') return '-';
        const text = String(value).trim();
        if (!text) return '-';
        if (/^\d{10}$/.test(text) || /^\d{13}$/.test(text)) {
            const ts = text.length === 13 ? Number(text) : Number(text) * 1000;
            const parsed = new Date(ts);
            return Number.isNaN(parsed.getTime()) ? text : parsed.toLocaleString();
        }
        const parsed = new Date(text);
        return Number.isNaN(parsed.getTime()) ? text : parsed.toLocaleString();
    }

    /**
     * Unix 秒级时间戳格式化
     */
    function formatUnixTimestamp(value) {
        if (!value) return '-';
        const numeric = Number(value);
        if (!Number.isFinite(numeric) || numeric <= 0) return '-';
        const parsed = new Date(numeric * 1000);
        return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
    }

    /**
     * 时间预设标签：取后端 summary 的 comment_time_preset_label
     */
    function formatTimePresetLabel(summary) {
        if (summary && summary.comment_time_preset_label) return summary.comment_time_preset_label;
        if (summary && summary.comment_time_preset_days) return `${summary.comment_time_preset_days}天内`;
        if (summary && (summary.comment_time_start || summary.comment_time_end)) {
            return `${formatSummaryTime(summary.comment_time_start)} ~ ${formatSummaryTime(summary.comment_time_end)}`;
        }
        return '不限';
    }

    /**
     * 将 Date 对象格式化为 `YYYY-MM-DD HH:MM:SS`
     */
    function formatDateTimeForApi(date) {
        if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
            return '';
        }
        const yyyy = date.getFullYear();
        const mm = String(date.getMonth() + 1).padStart(2, '0');
        const dd = String(date.getDate()).padStart(2, '0');
        const hh = String(date.getHours()).padStart(2, '0');
        const mi = String(date.getMinutes()).padStart(2, '0');
        const ss = String(date.getSeconds()).padStart(2, '0');
        return `${yyyy}-${mm}-${dd} ${hh}:${mi}:${ss}`;
    }

    /**
     * 根据 days 参数返回 ISO 起始时间字符串
     */
    function getTimePresetStartIso(daysValue) {
        const days = parseInt(daysValue, 10);
        if (!days || Number.isNaN(days) || days <= 0) return '';
        const dt = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
        return dt.toISOString();
    }

    /**
     * 分析面板时间格式化（与 formatSummaryTime 行为一致，但占位文本为「暂无」）
     */
    function formatAnalyticsTime(value) {
        if (!value) return '暂无';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return String(value);
        }
        return date.toLocaleString();
    }

    /**
     * 数字格式化：>10000 显示为「X.X万」
     */
    function formatNumber(num) {
        if (num >= 10000) {
            return (num / 10000).toFixed(1) + '万';
        }
        return num.toLocaleString();
    }

    /**
     * 响应时间格式化（分钟 -> 分钟/小时/天）
     */
    function formatResponseTime(minutes) {
        if (!minutes || minutes === 0) return '暂无数据';
        if (minutes < 60) return `${minutes.toFixed(0)}分钟`;
        if (minutes < 1440) return `${(minutes / 60).toFixed(1)}小时`;
        return `${(minutes / 1440).toFixed(1)}天`;
    }

    /**
     * 最后联系时间（相对时间）
     */
    function formatLastContact(time) {
        if (!time) return '-';
        try {
            const date = new Date(time);
            const now = new Date();
            const diff = now - date;
            const days = Math.floor(diff / (1000 * 60 * 60 * 24));
            if (days === 0) return '今天';
            if (days === 1) return '昨天';
            if (days < 7) return days + '天前';
            return date.toLocaleDateString();
        } catch (_e) {
            return time;
        }
    }

    /**
     * 文件大小格式化
     */
    function formatFileSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    // ============== 视频 / 客户来源 URL ==============

    /**
     * 从抖音 URL 抽取视频 ID
     */
    function extractVideoIdFromUrl(url) {
        const text = String(url || '').trim();
        if (!text) return '';
        const pathMatch = text.match(/\/(?:video|note)\/([0-9A-Za-z_-]+)/i);
        if (pathMatch) return pathMatch[1];
        const queryMatch = text.match(/[?&](?:aweme_id|modal_id)=([0-9A-Za-z_-]+)/i);
        return queryMatch ? queryMatch[1] : '';
    }

    /**
     * 视频类型（video / note）
     */
    function extractVideoKindFromUrl(url) {
        const text = String(url || '').trim().toLowerCase();
        if (!text) return 'video';
        if (text.includes('/note/')) return 'note';
        return 'video';
    }

    /**
     * 规范化抖音视频 URL
     */
    function getCanonicalVideoUrl(url) {
        const videoId = extractVideoIdFromUrl(url);
        if (!videoId) return '';
        const detailKind = extractVideoKindFromUrl(url);
        return `https://www.douyin.com/${detailKind}/${videoId}`;
    }

    /**
     * 客户视频显示信息聚合
     */
    function getCustomerVideoDisplay(customer) {
        customer = customer || {};
        const videoTitle = String(customer.video_title || '').trim();
        const authorName = String(customer.author_name || '').trim();
        const rawSourceVideoUrl = String(customer.first_source_video_url || customer.source_video_url || '').trim();
        const sourceVideoUrl = getCanonicalVideoUrl(rawSourceVideoUrl);
        const videoId = extractVideoIdFromUrl(sourceVideoUrl);
        const hasUrl = Boolean(rawSourceVideoUrl);
        const hasCanonicalUrl = Boolean(sourceVideoUrl);
        const needsWarning = hasUrl && !hasCanonicalUrl;
        const warningText = !hasUrl
            ? ''
            : (!hasCanonicalUrl ? '来源视频链接格式异常，已禁止直接打开' : '');

        return {
            title: videoTitle || (sourceVideoUrl ? (videoId ? `来源视频 ${videoId}` : '来源视频') : ''),
            subtitle: authorName || (sourceVideoUrl ? '已采集来源链接' : '-'),
            sourceVideoUrl,
            canOpen: hasCanonicalUrl,
            needsWarning,
            warningText,
        };
    }

    function getPreferredCustomerVideoUrl(customer) {
        return getCustomerVideoDisplay(customer).sourceVideoUrl || '';
    }

    /**
     * 打开客户来源视频（由后端跳转按钮调用）
     */
    function openCustomerVideoSource(button) {
        const videoUrl = String(button && button.dataset ? button.dataset.videoUrl || '' : '').trim();
        const needsWarning = String(button && button.dataset ? button.dataset.videoWarning || '' : '').trim() === '1';
        const warningText = String(button && button.dataset ? button.dataset.videoWarningText || '' : '').trim();
        if (!videoUrl) return;
        if (needsWarning) {
            const confirmed = window.confirm(warningText || '该来源视频可能已失效，继续打开后可能跳转到抖音推荐流。是否继续？');
            if (!confirmed) return;
        }
        window.open(videoUrl, '_blank', 'noopener,noreferrer');
    }

    // ============== 状态 / 阶段 / 行业 / 标签 字典 ==============

    const STATUS_LABELS = {
        pending: '待发送私信',
        sent: '已发送私信',
        replied: '客户已回复',
        failed: '失败',
    };

    const INTERACT_STATUS_LABELS = {
        pending: '未互动',
        interacted: '已互动',
        failed: '互动失败',
    };

    const SIZE_LABELS = {
        enterprise: '大型企业',
        mid_market: '中型企业',
        smb: '中小企业',
        startup: '创业公司',
        unknown: '未知',
    };

    const INDUSTRY_LABELS = {
        ecommerce: '电商',
        education: '教育',
        finance: '金融',
        technology: '科技',
        retail: '零售',
        services: '服务',
        healthcare: '医疗',
        manufacturing: '制造',
        realestate: '房产',
        unknown: '未知',
    };

    const STAGE_LABELS = {
        awareness: '认知阶段',
        interest: '兴趣阶段',
        evaluation: '评估阶段',
        decision: '决策阶段',
        purchase: '购买阶段',
    };

    const STAGE_SHORT_LABELS = {
        awareness: '认知',
        interest: '兴趣',
        evaluation: '评估',
        decision: '决策',
        purchase: '购买',
    };

    const STYLE_LABELS = {
        analytical: '分析型',
        directive: '指令型',
        conceptual: '概念型',
        behavioral: '行为型',
        unknown: '未知',
    };

    const COMM_LABELS = {
        formal: '正式沟通',
        casual: '轻松沟通',
        technical: '技术导向',
        business: '业务导向',
        unknown: '未知',
    };

    const ENGAGEMENT_LABELS = {
        active: '积极互动',
        moderate: '适度互动',
        passive: '被动互动',
        none: '无互动',
    };

    const BUDGET_LABELS = {
        high: '高预算',
        medium: '中等预算',
        low: '低预算',
        unknown: '未知',
    };

    const PRIORITY_LABELS = {
        high: '高优先级',
        medium: '中优先级',
        low: '低优先级',
    };

    const TIMING_LABELS = {
        immediate: '立即跟进',
        within_week: '本周跟进',
        normal: '正常跟进',
        nurture: '培育跟进',
    };

    const URGENCY_LABELS = {
        immediate: '紧急',
        short: '短期',
        medium: '中期',
        long: '长期',
        unknown: '未知',
    };

    const DIRECTION_LABELS = {
        inbound: '客户消息',
        outbound: '系统回复',
        unknown: '未知',
    };

    const LEAD_SCORE_LABELS = {
        hot: '热线索',
        warm: '温线索',
        cool: '冷启线索',
        cold: '低优先级',
    };

    const COMPLETION_STATUS_LABELS = {
        completed_full: '完整完成',
        completed_incremental: '增量完成',
        completed_quota_limited: '达到配额后完成',
        partial_due_to_limits: '部分完成',
        partial_due_to_risk: '风控中断',
        stopped_by_user: '手动停止',
        running: '执行中',
    };

    const TERMINATION_REASON_LABELS = {
        comment_end_reached: '评论翻页结束',
        top_level_comment_end_reached: '一级评论抓取结束',
        lead_quota_reached: '达到用户配额',
        history_cutoff_reached: '命中历史截止点',
        page_popup_interrupted: '页面弹窗中断',
        video_unavailable: '视频不可用',
        redirected_to_other_video: '跳转到其他视频',
        risk_control_page: '命中页面风控',
        risk_control_blocked_response: '接口风控拦截',
        stop_requested: '手动停止',
        loop_finished_without_end: '循环结束但未确认末页',
        exception: '执行异常',
    };

    const AUTO_REPLY_FAILURE_LABELS = {
        invalid_target: '目标数据不完整',
        target_not_found: '未定位到目标楼层',
        target_ambiguous: '目标评论匹配歧义',
        reply_button_missing: '未找到回复按钮',
        reply_button_click_failed: '回复按钮点击失败',
        reply_button_not_effective: '点击回复后未进入回复状态',
        reply_button_lookup_failed: '回复按钮定位异常',
        reply_editor_missing: '未找到回复输入框',
        reply_context_missing: '未进入楼层回复上下文',
        reply_fill_failed: '回复内容写入失败',
        send_button_missing: '未找到发送按钮',
        submit_request_rejected: '发布接口返回失败',
        submit_verify_failed: '发送后状态校验失败',
        execution_exception: '执行器异常',
        unknown: '未知失败',
    };

    const LICENSE_TYPE_LABELS = {
        trial_7d: '7天试用',
        month_1: '1个月',
        month_3: '3个月',
        month_6: '6个月',
        year_1: '1年',
        permanent: '永久授权',
    };

    // ============== 状态/阶段/行业 标签查询函数 ==============

    function getStatusLabel(status) {
        return STATUS_LABELS[status] || escapeHtml(String(status || '未设置'));
    }

    function getInteractStatusLabel(status) {
        return INTERACT_STATUS_LABELS[status] || escapeHtml(String(status || '未设置'));
    }

    function getStatusBadge(status) {
        switch (status) {
            case 'sent': return 'bg-success';
            case 'replied': return 'bg-primary';
            case 'pending': return 'bg-warning text-dark';
            case 'failed': return 'bg-danger';
            default: return 'bg-secondary';
        }
    }

    function getInteractStatusBadge(status) {
        switch (status) {
            case 'interacted': return 'bg-info text-dark';
            case 'failed': return 'bg-danger';
            case 'pending': return 'bg-secondary';
            default: return 'bg-secondary';
        }
    }

    function getSizeLabel(size) {
        return SIZE_LABELS[size] || escapeHtml(String(size || '未知'));
    }

    function getIndustryLabel(industry) {
        return INDUSTRY_LABELS[industry] || escapeHtml(String(industry || '未知'));
    }

    function getStageLabel(stage) {
        return STAGE_LABELS[stage] || escapeHtml(String(stage || '未知'));
    }

    function getStyleLabel(style) {
        return STYLE_LABELS[style] || escapeHtml(String(style || '未知'));
    }

    function getCommLabel(pref) {
        return COMM_LABELS[pref] || escapeHtml(String(pref || '未知'));
    }

    function getEngagementLabel(pattern) {
        return ENGAGEMENT_LABELS[pattern] || escapeHtml(String(pattern || '未知'));
    }

    function getBudgetLabel(range) {
        return BUDGET_LABELS[range] || escapeHtml(String(range || '未知'));
    }

    function getPriorityLabel(priority) {
        return PRIORITY_LABELS[priority] || escapeHtml(String(priority || '未知'));
    }

    function getTimingLabel(timing) {
        return TIMING_LABELS[timing] || escapeHtml(String(timing || '未知'));
    }

    function getTimelineUrgencyLabel(urgency) {
        return URGENCY_LABELS[urgency] || escapeHtml(String(urgency || '未知'));
    }

    function getDirectionLabel(direction) {
        return DIRECTION_LABELS[direction] || escapeHtml(String(direction || '未知'));
    }

    function getDirectionBadgeClass(direction) {
        if (direction === 'inbound') return 'bg-primary';
        if (direction === 'outbound') return 'bg-success';
        return 'bg-secondary';
    }

    function getIntentLevelBadgeClass(level) {
        switch (String(level || '').toUpperCase()) {
            case 'A': return 'bg-danger';
            case 'B': return 'bg-warning text-dark';
            case 'C': return 'bg-info text-dark';
            case 'D': return 'bg-secondary';
            case 'E': return 'bg-dark';
            default: return 'bg-light text-dark';
        }
    }

    function getInteractionHeatBadgeClass(label) {
        switch (String(label || '').toLowerCase()) {
            case '高': return 'bg-danger';
            case '中': return 'bg-warning text-dark';
            case '低': return 'bg-secondary';
            default: return 'bg-light text-dark';
        }
    }

    function formatLeadScoreLabel(leadScore) {
        return LEAD_SCORE_LABELS[leadScore] || leadScore || '未知';
    }

    function formatCompletionStatusLabel(status) {
        const normalized = String(status || '').trim();
        return COMPLETION_STATUS_LABELS[normalized] || normalized || '未知';
    }

    function formatTerminationReasonLabel(reason) {
        const normalized = String(reason || '').trim();
        return TERMINATION_REASON_LABELS[normalized] || normalized || '未知';
    }

    function formatAutoReplyFailureReason(reason) {
        const normalized = String(reason || '').trim();
        return AUTO_REPLY_FAILURE_LABELS[normalized] || normalized || '未知失败';
    }

    function getLicenseTypeLabel(licenseType) {
        return LICENSE_TYPE_LABELS[licenseType] || '授权';
    }

    function formatMachineHashPreview(value) {
        const normalized = String(value || '').trim();
        if (!normalized) return '-';
        if (normalized.length <= 16) return normalized;
        return `${normalized.slice(0, 8)}...${normalized.slice(-8)}`;
    }

    // ============== 复合 HTML 渲染（轻度 DOM 依赖） ==============

    /**
     * 渲染客户私信 / 互动 状态徽章组合
     */
    function renderCustomerStatusInfo(customer) {
        customer = customer || {};
        const directStatus = customer.status || 'pending';
        const interactStatus = customer.interact_status || 'pending';
        return `
        <div class="d-flex flex-column gap-1">
            <div><span class="badge ${getStatusBadge(directStatus)}">私信 ${escapeHtml(getStatusLabel(directStatus))}</span></div>
            <div><span class="badge ${getInteractStatusBadge(interactStatus)}">互动 ${escapeHtml(getInteractStatusLabel(interactStatus))}</span></div>
        </div>
    `;
    }

    /**
     * 渲染标签徽章集合
     */
    function renderTagBadges(tags, badgeClass, emptyText) {
        badgeClass = badgeClass || 'bg-secondary';
        emptyText = emptyText || '暂无标签';
        const normalized = Array.isArray(tags)
            ? tags.map((tag) => String(tag || '').trim()).filter(Boolean)
            : [];
        return normalized.length
            ? normalized.map((tag) => `<span class="badge ${badgeClass} me-1 mb-1">${escapeHtml(tag)}</span>`).join('')
            : `<span class="text-muted">${escapeHtml(emptyText)}</span>`;
    }

    /**
     * 渲染购买信号徽章集合
     */
    function formatSignalsList(signals) {
        if (!signals || signals.length === 0) {
            return '<p class="text-muted">暂未检测到购买信号</p>';
        }
        const signalLabels = {
            explicit_intent: { label: '明确意向', icon: '🎯', color: 'success' },
            price_inquiry: { label: '询价', icon: '💰', color: 'warning' },
            demo_request: { label: '请求演示', icon: '🖥️', color: 'info' },
            contact_request: { label: '请求联系', icon: '📞', color: 'primary' },
            budget_discussion: { label: '预算讨论', icon: '💵', color: 'success' },
            decision_maker: { label: '决策者', icon: '👔', color: 'danger' },
            timeline_mention: { label: '时间线提及', icon: '📅', color: 'info' },
            competitor_comparison: { label: '竞品对比', icon: '⚖️', color: 'warning' },
            feature_inquiry: { label: '功能咨询', icon: '🔧', color: 'info' },
            objection: { label: '异议表达', icon: '❓', color: 'secondary' },
        };
        return signals.map((signal) => {
            const info = signalLabels[signal] || { label: signal, icon: '📌', color: 'secondary' };
            return `<span class="badge bg-${info.color} me-1 mb-1">${info.icon} ${info.label}</span>`;
        }).join('');
    }

    /**
     * 渲染信号徽章（用于客户列表等紧凑区域）
     */
    function formatSignals(signals) {
        if (!signals || signals.length === 0) return '-';
        const signalLabels = {
            explicit_intent: '明确意向',
            price_inquiry: '询价',
            demo_request: '请求演示',
            contact_request: '请求联系',
            budget_discussion: '预算讨论',
            decision_maker: '决策者',
        };
        return signals.slice(0, 2)
            .map((s) => `<span class="badge bg-secondary me-1">${signalLabels[s] || escapeHtml(String(s))}</span>`)
            .join('');
    }

    // ============== 进度/原因汇总（HTML 字符串） ==============

    function buildAutoReplyProgressDetail(summary) {
        if (!summary || !summary.auto_reply_enabled) return '';

        const attempted = Number(summary.auto_reply_attempted || 0);
        const success = Number(summary.auto_reply_success || 0);
        const failed = Number(summary.auto_reply_failed != null
            ? summary.auto_reply_failed
            : Math.max(attempted - success, 0));
        const replyQuota = Number(summary.auto_reply_quota || 0);
        const remainingReplyQuota = Number(summary.auto_reply_remaining_quota || 0);
        const reasonEntries = Object.entries(summary.auto_reply_failure_reasons || {})
            .filter(([, count]) => Number(count) > 0)
            .sort((a, b) => Number(b[1]) - Number(a[1]));
        const sample = Array.isArray(summary.auto_reply_failure_samples) ? summary.auto_reply_failure_samples[0] : null;
        const diagnostics = summary.auto_reply_diagnostics || {};
        const contextRecovered = Number(diagnostics.context_recovered_count || 0);
        const parentAnchorMatched = Number(diagnostics.parent_anchor_matched_count || 0);
        const surfaceScopeHit = Number(diagnostics.surface_scope_hit_count || 0);
        const documentFallbackHit = Number(diagnostics.document_fallback_hit_count || 0);

        if (
            attempted <= 0
            && reasonEntries.length === 0
            && !summary.auto_reply_limit_reached
            && !summary.auto_reply_quota_exhausted
        ) {
            return '';
        }

        const parts = [
            `评论下回复: 成功 ${success}/${attempted}${failed > 0 ? `，失败 ${failed}` : ''}`,
        ];

        if (reasonEntries.length > 0) {
            const reasonsText = reasonEntries
                .slice(0, 3)
                .map(([reason, count]) => `${formatAutoReplyFailureReason(reason)} ${count}`)
                .join('，');
            parts.push(`失败原因: ${reasonsText}`);
        }

        if (sample) {
            const sampleTarget = sample.nickname || sample.comment_id || sample.aweme_id || '目标评论';
            const sampleMessage = sample.message || formatAutoReplyFailureReason(sample.reason);
            const sampleStage = String(sample.stage || '').trim();
            const sampleDiag = sample.diagnostics || {};
            const diagBits = [];
            if (sampleStage) diagBits.push(`阶段 ${sampleStage}`);
            if (sampleDiag.surface_selector) diagBits.push(`容器 ${sampleDiag.surface_selector}`);
            if (sampleDiag.parent_anchor_matched) diagBits.push('父锚点已命中');
            if (sampleDiag.context_recovered) diagBits.push('已做上下文恢复');
            parts.push(`最近失败: ${sampleTarget} - ${sampleMessage}${diagBits.length ? `（${diagBits.join('，')}）` : ''}`);
        }

        if (contextRecovered > 0 || parentAnchorMatched > 0 || surfaceScopeHit > 0 || documentFallbackHit > 0) {
            const diagSummary = [];
            if (contextRecovered > 0) diagSummary.push(`上下文恢复 ${contextRecovered}`);
            if (parentAnchorMatched > 0) diagSummary.push(`父锚点命中 ${parentAnchorMatched}`);
            if (surfaceScopeHit > 0) diagSummary.push(`评论容器命中 ${surfaceScopeHit}`);
            if (documentFallbackHit > 0) diagSummary.push(`全页兜底 ${documentFallbackHit}`);
            if (diagSummary.length) {
                parts.push(`定位诊断: ${diagSummary.join('，')}`);
            }
        }

        if (summary.auto_reply_limit_reached) {
            parts.push('已触发评论频控上限，后续仅抓取，不再回复');
        }
        if (summary.auto_reply_quota_exhausted) {
            const quotaText = replyQuota > 0
                ? `（${Math.max(replyQuota - remainingReplyQuota, 0)}/${replyQuota}）`
                : '';
            parts.push(`评论下回复数量已用完${quotaText}，后续仅抓取，不再回复`);
        }

        return parts.map((item) => escapeHtml(item)).join('<br>');
    }

    function buildSearchQueueProgressDetail(summary) {
        if (!summary) return '';
        const processedVideos = Number(
            summary.videos_processed
            || summary.processed_count
            || summary.completed_videos
            || 0
        );
        const crawledComments = Number(
            summary.comments_crawled
            || summary.total_comments
            || (Number(summary.top_level_comments || 0) + Number(summary.reply_comments || 0))
        );
        return `已抓取视频 ${processedVideos} 个，已抓取评论 ${crawledComments} 条`;
    }

    // ============== DOM 相关：时间范围构建 ==============

    /**
     * 从搜索表单的 comment_time_range 输入构建 API 时间范围
     */
    function buildSearchCommentTimeRange() {
        const rangeValue = (document.getElementById('comment_time_range') && document.getElementById('comment_time_range').value) || '';
        if (!rangeValue) {
            return { valid: true, comment_time_start: '', comment_time_end: '' };
        }
        const parts = rangeValue.split(' 至 ');
        let startDate = parts[0] ? new Date(parts[0]) : null;
        let endDate = parts[1] ? new Date(parts[1]) : startDate;
        if (!startDate || Number.isNaN(startDate.getTime())) {
            return { valid: true, comment_time_start: '', comment_time_end: '' };
        }
        if (!endDate || Number.isNaN(endDate.getTime())) {
            endDate = startDate;
        }
        endDate.setHours(23, 59, 59, 999);
        startDate.setHours(0, 0, 0, 0);
        return {
            valid: true,
            comment_time_start: formatDateTimeForApi(startDate),
            comment_time_end: formatDateTimeForApi(endDate),
        };
    }

    function buildCustomerCommentTimeRange() {
        const rangeValue = (document.getElementById('filter-comment-time') && document.getElementById('filter-comment-time').value) || '';
        if (!rangeValue) {
            return { valid: true, comment_time_start: '', comment_time_end: '' };
        }
        const parts = rangeValue.split(' 至 ');
        let startDate = parts[0] ? new Date(parts[0]) : null;
        let endDate = parts[1] ? new Date(parts[1]) : startDate;
        if (!startDate || Number.isNaN(startDate.getTime())) {
            return { valid: true, comment_time_start: '', comment_time_end: '' };
        }
        if (!endDate || Number.isNaN(endDate.getTime())) {
            endDate = startDate;
        }
        endDate.setHours(23, 59, 59, 999);
        startDate.setHours(0, 0, 0, 0);
        return {
            valid: true,
            comment_time_start: formatDateTimeForApi(startDate),
            comment_time_end: formatDateTimeForApi(endDate),
        };
    }

    // ============== 命名空间导出 ==============

    const HKUtils = {
        // HTML / 数值
        escapeHtml,
        sanitizePercent,
        // 时间
        formatSummaryTime,
        formatCustomerCommentTime,
        formatUnixTimestamp,
        formatTimePresetLabel,
        formatDateTimeForApi,
        getTimePresetStartIso,
        formatAnalyticsTime,
        formatNumber,
        formatResponseTime,
        formatLastContact,
        formatFileSize,
        // 视频 / URL
        extractVideoIdFromUrl,
        extractVideoKindFromUrl,
        getCanonicalVideoUrl,
        getCustomerVideoDisplay,
        getPreferredCustomerVideoUrl,
        openCustomerVideoSource,
        // 状态/阶段/行业
        getStatusLabel,
        getInteractStatusLabel,
        getStatusBadge,
        getInteractStatusBadge,
        getSizeLabel,
        getIndustryLabel,
        getStageLabel,
        getStyleLabel,
        getCommLabel,
        getEngagementLabel,
        getBudgetLabel,
        getPriorityLabel,
        getTimingLabel,
        getTimelineUrgencyLabel,
        getDirectionLabel,
        getDirectionBadgeClass,
        getIntentLevelBadgeClass,
        getInteractionHeatBadgeClass,
        getLicenseTypeLabel,
        formatMachineHashPreview,
        formatLeadScoreLabel,
        formatCompletionStatusLabel,
        formatTerminationReasonLabel,
        formatAutoReplyFailureReason,
        // HTML 渲染
        renderCustomerStatusInfo,
        renderTagBadges,
        formatSignalsList,
        formatSignals,
        // 进度汇总
        buildAutoReplyProgressDetail,
        buildSearchQueueProgressDetail,
        // 时间范围构建
        buildSearchCommentTimeRange,
        buildCustomerCommentTimeRange,
        // 字典常量
        DICT: {
            STATUS_LABELS,
            INTERACT_STATUS_LABELS,
            SIZE_LABELS,
            INDUSTRY_LABELS,
            STAGE_LABELS,
            STAGE_SHORT_LABELS,
            STYLE_LABELS,
            COMM_LABELS,
            ENGAGEMENT_LABELS,
            BUDGET_LABELS,
            PRIORITY_LABELS,
            TIMING_LABELS,
            URGENCY_LABELS,
            DIRECTION_LABELS,
            LEAD_SCORE_LABELS,
            COMPLETION_STATUS_LABELS,
            TERMINATION_REASON_LABELS,
            AUTO_REPLY_FAILURE_LABELS,
            LICENSE_TYPE_LABELS,
        },
    };

    global.HKUtils = HKUtils;
})(typeof window !== 'undefined' ? window : globalThis);
