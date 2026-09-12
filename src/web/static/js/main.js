/**
 * 主业务逻辑 - 从 index.html 模块化拆分
 *
 * 模块分区:
 *   全局变量与状态轮询 | 任务控制 | 客户管理
 *   消息同步与导出 | 会话列表与消息显示
 *   消息发送与意向分析 | 数据分析与图表
 *   高意向客户与画像 | 高级评分与WebSocket
 *   学习系统 | 知识库CRUD | 企业管理
 *   文件上传 | 知识图谱
 *
 * 阶段 D·D-4：已抽离以下纯函数/工具到 modules/ 命名空间，
 *   index.html 必须先于本文件加载 hk-utils / hk-api / hk-ui：
 *     - HKUtils：escapeHtml / sanitizePercent / 时间格式化 / 状态标签字典 / 视频URL / 进度汇总等
 *     - HKApi  ：fetchWithTimeout / fetchJson / pollBackoff / getRequestErrorMessage
 *     - HKUI   ：showToast / setButtonLoading / bindModalAccessibility / 徽章更新器
 *   本文件保留同名函数定义以维持完全向后兼容，行为与上述模块等价。
 */

const API_BASE = '/api';
let isMonitoring = false;
let isRestarting = false;
let currentLicenseStatus = window.__HUOKE_LICENSE__ || null;
let currentRemoteControlStatus = null;
let currentDingTalkConfig = null;
let lastObservedTask = 'Idle';
let statusPollTimer = null;
let pageIsUnloading = false;
let searchTaskSubmitting = false;
let sendTaskSubmitting = false;
let interactTaskSubmitting = false;
let crawlerStopPending = false;
const SEARCH_START_TIMEOUT_MS = 125000;

function isCrawlerTaskSubmitting() {
    return searchTaskSubmitting || sendTaskSubmitting || interactTaskSubmitting || crawlerStopPending;
}

function syncCrawlerStopButtonsPendingState(pending) {
    const stopButtons = [
        document.getElementById('btn-search-stop'),
        document.getElementById('btn-send-stop'),
        document.getElementById('interact-stop-btn'),
    ];
    stopButtons.forEach((btn) => {
        if (!btn) return;
        if (!btn.dataset.originalText) {
            btn.dataset.originalText = btn.textContent.trim() || '停止运行';
        }
        if (pending) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>停止中...';
        } else {
            btn.disabled = false;
            btn.textContent = btn.dataset.originalText || btn.textContent;
        }
    });
}

function applyCrawlerStartSharedCooldown(activeTask = '') {
    const searchStartBtn = document.getElementById('btn-search-start');
    const sendStartBtn = document.getElementById('btn-send-start');
    const interactBtn = document.getElementById('customer-interact-btn');
    const interactSubmitBtn = document.getElementById('interact-submit-btn');
    const interactStopBtn = document.getElementById('interact-stop-btn');

    if (searchStartBtn && activeTask !== 'search') {
        searchStartBtn.disabled = true;
    }
    if (sendStartBtn && activeTask !== 'send') {
        sendStartBtn.disabled = true;
    }
    if (interactBtn && activeTask !== 'interact') {
        interactBtn.disabled = true;
    }
    if (interactSubmitBtn && activeTask !== 'interact') {
        interactSubmitBtn.disabled = true;
    }
    if (interactStopBtn && activeTask !== 'interact') {
        interactStopBtn.disabled = true;
    }
}

function setCrawlerTaskButtonVisibility(sharedTaskActive) {
    const searchStartBtn = document.getElementById('btn-search-start');
    const searchStopBtn = document.getElementById('btn-search-stop');
    const sendStartBtn = document.getElementById('btn-send-start');
    const sendStopBtn = document.getElementById('btn-send-stop');

    if (sharedTaskActive) {
        searchStartBtn?.classList.add('d-none');
        searchStopBtn?.classList.remove('d-none');
        sendStartBtn?.classList.add('d-none');
        sendStopBtn?.classList.remove('d-none');
        return;
    }

    searchStartBtn?.classList.remove('d-none');
    searchStopBtn?.classList.add('d-none');
    sendStartBtn?.classList.remove('d-none');
    sendStopBtn?.classList.add('d-none');
}

function syncInteractTaskButtons({ taskActive = false } = {}) {
    const interactBtn = document.getElementById('customer-interact-btn');
    const interactSubmitBtn = document.getElementById('interact-submit-btn');
    const interactStopBtn = document.getElementById('interact-stop-btn');

    if (interactBtn) {
        if (taskActive) {
            interactBtn.disabled = true;
        } else {
            syncCustomerSelectionUI();
        }
    }

    if (!interactSubmitBtn || !interactStopBtn || interactTaskSubmitting) {
        return;
    }

    if (!interactSubmitBtn.dataset.idleText) {
        interactSubmitBtn.dataset.idleText = interactSubmitBtn.textContent.trim() || '开始执行';
    }

    if (taskActive) {
        interactSubmitBtn.classList.add('d-none');
        interactStopBtn.classList.remove('d-none');
        interactStopBtn.disabled = false;
        return;
    }

    interactSubmitBtn.classList.remove('d-none');
    interactStopBtn.classList.add('d-none');
    interactSubmitBtn.disabled = false;
    interactSubmitBtn.textContent = interactSubmitBtn.dataset.idleText;
}

/**
 * 带超时的fetch请求
 * @param {string} url - 请求URL
 * @param {object} options - fetch选项
 * @param {number} timeout - 超时时间(毫秒)，默认15秒
 */
async function fetchWithTimeout(url, options = {}, timeout = 15000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);
    try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        if (!response.ok) {
            let errorPayload = null;
            let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
            try {
                const contentType = response.headers.get('content-type') || '';
                if (contentType.includes('application/json')) {
                    errorPayload = await response.json();
                } else {
                    const text = await response.text();
                    errorPayload = text ? { message: text } : null;
                }
            } catch (_parseError) {
                errorPayload = null;
            }

            if (errorPayload?.message) {
                errorMessage = errorPayload.message;
            } else if (errorPayload?.detail) {
                errorMessage = errorPayload.detail;
            }

            const error = new Error(errorMessage);
            error.status = response.status;
            error.responseData = errorPayload;
            throw error;
        }
        return response;
    } catch (e) {
        if (e.name === 'AbortError') {
            throw new DOMException(`Request timeout after ${timeout}ms`, 'AbortError');
        }
        throw e;
    } finally {
        clearTimeout(timeoutId);
    }
}

function getRequestErrorMessage(error, fallback = '请求失败') {
    const responseData = error?.responseData || {};
    const licenseStatus = responseData?.license_status || {};
    const remoteControlStatus = responseData?.remote_control_status || {};

    if (licenseStatus && Object.keys(licenseStatus).length > 0) {
        currentLicenseStatus = licenseStatus;
        updateLicenseBadge(currentLicenseStatus);
        updateLicenseModalStatus(currentLicenseStatus);
    }

    if (licenseStatus?.status === 'expired') {
        return '授权已到期，请和供应商联系获取使用权限';
    }

    if (licenseStatus?.status === 'clock_rollback') {
        return '检测到系统时间被回拨，请校准电脑时间后重试，或联系供应商获取使用权限';
    }

    if (licenseStatus?.status === 'missing') {
        return '软件尚未激活，请先激活后再使用该功能';
    }

    if (licenseStatus?.status === 'invalid' || licenseStatus?.status === 'machine_mismatch') {
        return '授权无效，请使用新版激活码重新激活，或联系供应商获取使用权限';
    }

    if (responseData?.message) {
        if (remoteControlStatus && Object.keys(remoteControlStatus).length > 0) {
            currentRemoteControlStatus = remoteControlStatus;
            updateRemoteControlBadge(currentRemoteControlStatus);
        }
        return responseData.message;
    }

    if (error?.message) {
        return error.message;
    }

    return fallback;
}

/**
 * 轮询退避管理器
 */
const pollBackoff = {
    baseInterval: 5000,
    maxInterval: 30000,
    currentInterval: 5000,
    consecutiveErrors: 0,
    
    onSuccess() {
        this.consecutiveErrors = 0;
        this.currentInterval = this.baseInterval;
    },
    
    onError() {
        this.consecutiveErrors++;
        this.currentInterval = Math.min(
            this.baseInterval * Math.pow(2, this.consecutiveErrors - 1),
            this.maxInterval
        );
    },
    
    getInterval() {
        return this.currentInterval;
    }
};

// ========== Toast提示组件（统一使用增强版） ==========

/**
 * 显示Toast提示
 * @param {string} message - 提示消息
 * @param {string} type - 类型: success, error, warning, info
 */
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container') || createToastContainer();

    const icons = {
        'success': '✅',
        'error': '❌',
        'warning': '⚠️',
        'info': 'ℹ️'
    };

    const colors = {
        'success': 'bg-success',
        'error': 'bg-danger',
        'warning': 'bg-warning text-dark',
        'info': 'bg-info'
    };

    const toastId = 'toast-' + Date.now();
    const toastHtml = `
        <div id="${toastId}" class="toast show toast-min-width" role="alert">
            <div class="toast-header ${colors[type] || 'bg-info'} text-white">
                <span class="me-2">${icons[type] || 'ℹ️'}</span>
                <strong class="me-auto">${type === 'success' ? '成功' : type === 'error' ? '错误' : type === 'warning' ? '警告' : '提示'}</strong>
                <button type="button" class="btn-close btn-close-white" onclick="closeToast('${toastId}')"></button>
            </div>
            <div class="toast-body">
                ${escapeHtml(message)}
            </div>
        </div>
    `;

    container.insertAdjacentHTML('beforeend', toastHtml);

    setTimeout(() => {
        closeToast(toastId);
    }, 3000);
}

function bindModalAccessibility(modalElement, options = {}) {
    if (!modalElement || modalElement.dataset.accessibilityBound === 'true') {
        return modalElement;
    }

    const {
        removeOnHidden = false,
        restoreFocus = true,
    } = options;

    const previousActiveElement = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;

    modalElement.dataset.accessibilityBound = 'true';

    modalElement.addEventListener('hide.bs.modal', function() {
        const activeElement = document.activeElement;
        if (activeElement instanceof HTMLElement && modalElement.contains(activeElement)) {
            activeElement.blur();
        }
    });

    modalElement.addEventListener('hidden.bs.modal', function() {
        if (restoreFocus && previousActiveElement && document.contains(previousActiveElement)) {
            try {
                previousActiveElement.focus();
            } catch (e) {
                document.body.focus?.();
            }
        } else {
            document.body.focus?.();
        }

        if (removeOnHidden) {
            modalElement.remove();
        }
    });

    return modalElement;
}

function getLicenseTypeLabel(licenseType) {
    const typeLabels = {
        trial_7d: '7天试用',
        month_1: '1个月',
        month_3: '3个月',
        month_6: '6个月',
        year_1: '1年',
        permanent: '永久授权'
    };
    return typeLabels[licenseType] || '授权';
}

function formatMachineHashPreview(value) {
    const normalized = String(value || '').trim();
    if (!normalized) return '-';
    if (normalized.length <= 16) return normalized;
    return `${normalized.slice(0, 8)}...${normalized.slice(-8)}`;
}

function updateLicenseBadge(licenseState) {
    const badge = document.getElementById('license-status');
    if (!badge || !licenseState) return;

    if (licenseState.status === 'expired') {
        badge.className = 'badge bg-danger status-badge';
        badge.textContent = '授权已到期';
        return;
    }

    if (licenseState.status === 'clock_rollback') {
        badge.className = 'badge bg-danger status-badge';
        badge.textContent = '检测到时间回拨';
        return;
    }

    if (!licenseState.valid) {
        badge.className = 'badge bg-danger status-badge';
        badge.textContent = '未激活';
        return;
    }

    const licenseTypeLabel = licenseState.license_type_label || getLicenseTypeLabel(licenseState.license_type);
    if (licenseState.license_type !== 'permanent') {
        badge.className = 'badge bg-warning text-dark status-badge';
        const days = Number.isFinite(licenseState.days_remaining) ? licenseState.days_remaining : '-';
        badge.textContent = `${licenseTypeLabel}(${days}天)`;
        return;
    }

    badge.className = 'badge bg-success status-badge';
    badge.textContent = licenseTypeLabel;
}

function updateLicenseModalStatus(licenseState) {
    const el = document.getElementById('license-modal-status');
    if (!el || !licenseState) return;

    if (licenseState.status === 'expired') {
        el.textContent = '授权已到期，请和供应商联系获取使用权限';
        return;
    }

    if (licenseState.status === 'clock_rollback') {
        el.textContent = '检测到系统时间被回拨，请校准电脑时间后重试，或联系供应商获取使用权限';
        return;
    }

    if (!licenseState.valid) {
        el.textContent = `未激活，当前设备 ${formatMachineHashPreview(licenseState.machine_hash)}`;
        return;
    }

    const licenseTypeLabel = licenseState.license_type_label || getLicenseTypeLabel(licenseState.license_type);
    const phoneNumber = licenseState.phone_number || '-';
    const licensedMachineHash = formatMachineHashPreview(licenseState.licensed_machine_hash);
    if (licenseState.license_type !== 'permanent') {
        const days = Number.isFinite(licenseState.days_remaining) ? licenseState.days_remaining : '-';
        el.textContent = `${licenseTypeLabel}，剩余 ${days} 天，绑定手机号 ${phoneNumber}，绑定设备 ${licensedMachineHash}`;
        return;
    }

    el.textContent = `${licenseTypeLabel}，绑定手机号 ${phoneNumber}，绑定设备 ${licensedMachineHash}`;
}

function updateRemoteControlBadge(remoteState) {
    const badge = document.getElementById('remote-control-status');
    if (!badge || !remoteState) return;

    if (!remoteState.valid) {
        badge.className = 'badge bg-danger status-badge d-none';
        badge.textContent = '远控异常';
        return;
    }

    if (!remoteState.enabled) {
        badge.className = 'badge bg-secondary status-badge d-none';
        badge.textContent = '远控未配置';
        return;
    }

    if (!remoteState.app_enabled) {
        badge.className = 'badge bg-danger status-badge d-none';
        badge.textContent = '远控已禁用';
        return;
    }

    const limitedFeatures = [];
    if (!remoteState.crawler_enabled) limitedFeatures.push('爬取');
    if (!remoteState.monitor_enabled) limitedFeatures.push('监听');
    if (!remoteState.auto_reply_enabled) limitedFeatures.push('回复');

    if (limitedFeatures.length > 0) {
        badge.className = 'badge bg-warning text-dark status-badge d-none';
        badge.textContent = `远控限制:${limitedFeatures.join('/')}`;
        return;
    }

    badge.className = 'badge bg-success status-badge d-none';
    badge.textContent = '远控正常';
}

async function refreshLicenseStatus() {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/license/status`);
        const data = await res.json();
        if (data?.success && data?.data) {
            currentLicenseStatus = data.data;
            updateLicenseBadge(currentLicenseStatus);
            updateLicenseModalStatus(currentLicenseStatus);
        }
    } catch (e) {
        if (currentLicenseStatus) {
            updateLicenseBadge(currentLicenseStatus);
            updateLicenseModalStatus(currentLicenseStatus);
        }
    }
}

function fillLicenseCrawlLimitConfig(payload = {}) {
    const config = payload?.config || payload || {};
    const status = payload?.status || {};
    document.getElementById('license-crawl-limit-enabled').checked = !!config?.enabled;
    document.getElementById('license-crawl-limit-daily').value = config?.daily_limit ?? '';
    const statusEl = document.getElementById('license-crawl-limit-status');
    if (statusEl) {
        statusEl.textContent = `今日已爬取 ${status?.today_scanned ?? 0} / ${status?.daily_limit ?? config?.daily_limit ?? '-'}${status?.limited ? '，已触发限制' : ''}`;
    }
}

function fillLicenseSendLimitConfig(payload = {}) {
    const config = payload?.config || payload || {};
    const status = payload?.status || {};
    document.getElementById('license-send-limit-enabled').checked = !!config?.enabled;
    document.getElementById('license-send-initial-hourly').value = config?.initial_hourly_limit ?? '';
    document.getElementById('license-send-initial-daily').value = config?.initial_daily_limit ?? '';
    document.getElementById('license-send-hourly-increment').value = config?.hourly_increment ?? '';
    document.getElementById('license-send-daily-increment').value = config?.daily_increment ?? '';
    document.getElementById('license-send-max-hourly').value = config?.max_hourly_limit ?? '';
    document.getElementById('license-send-max-daily').value = config?.max_daily_limit ?? '';
    document.getElementById('license-send-cycle-days').value = config?.increment_cycle_days ?? '';
    document.getElementById('license-send-retention-days').value = config?.event_retention_days ?? '';
    const statusEl = document.getElementById('license-send-limit-status');
    if (statusEl) {
        statusEl.textContent = `私信占用 每小时 ${status?.occupied_hourly_quota ?? 0}/${status?.effective_hourly_limit ?? '-'}，每天 ${status?.occupied_daily_quota ?? 0}/${status?.effective_daily_limit ?? '-'}`;
    }
}

function fillLicenseInteractLimitConfig(payload = {}) {
    const config = payload?.config || payload || {};
    const status = payload?.status || {};
    document.getElementById('license-interact-limit-enabled').checked = !!config?.enabled;
    document.getElementById('license-interact-min-interval').value = config?.min_interval_seconds ?? '';
    document.getElementById('license-interact-10m-limit').value = config?.rolling_10m_limit ?? '';
    document.getElementById('license-interact-24h-limit').value = config?.rolling_24h_limit ?? '';
    document.getElementById('license-interact-retention-days').value = config?.event_retention_days ?? '';
    const statusEl = document.getElementById('license-interact-limit-status');
    if (statusEl) {
        statusEl.textContent = `近10分钟 ${status?.rolling_10m_count ?? 0}/${status?.config?.rolling_10m_limit ?? config?.rolling_10m_limit ?? '-'}，近24小时 ${status?.rolling_24h_count ?? 0}/${status?.config?.rolling_24h_limit ?? config?.rolling_24h_limit ?? '-'}`;
    }
}

function buildSearchReplyLimitBlockedMessage(status = {}, config = {}) {
    const blockedWindow = String(status?.blocked_window || '').trim();
    if (blockedWindow === '10m') {
        return `已触发评论下回复 10 分钟上限：${status?.rolling_10m_count ?? 0}/${status?.config?.rolling_10m_limit ?? config?.rolling_10m_limit ?? '-'}`;
    }
    if (blockedWindow === '1h') {
        return `已触发评论下回复 1 小时上限：${status?.rolling_1h_count ?? 0}/${status?.config?.rolling_1h_limit ?? config?.rolling_1h_limit ?? '-'}`;
    }
    if (blockedWindow === '24h') {
        return `已触发评论下回复 24 小时上限：${status?.rolling_24h_count ?? 0}/${status?.config?.rolling_24h_limit ?? config?.rolling_24h_limit ?? '-'}`;
    }
    return '';
}

function applySearchReplyLimitStatus(status = {}) {
    const toggle = document.getElementById('enable_search_reply');
    const config = status?.config || {};
    const enabled = Boolean(status?.enabled ?? config?.enabled ?? true);
    const forcedOff = Boolean(status?.toggle_forced_off);
    if (toggle) {
        if (forcedOff) {
            toggle.checked = false;
        }
        toggle.disabled = forcedOff;
    }
    toggleSearchReplyConfig();
}

function fillLicenseSearchReplyLimitConfig(payload = {}) {
    const config = payload?.config || payload || {};
    const status = payload?.status || {};
    document.getElementById('license-search-reply-limit-enabled').checked = !!config?.enabled;
    document.getElementById('license-search-reply-10m-limit').value = config?.rolling_10m_limit ?? '';
    document.getElementById('license-search-reply-1h-limit').value = config?.rolling_1h_limit ?? '';
    document.getElementById('license-search-reply-24h-limit').value = config?.rolling_24h_limit ?? '';
    document.getElementById('license-search-reply-retention-days').value = config?.event_retention_days ?? '';
    const statusEl = document.getElementById('license-search-reply-limit-status');
    if (statusEl) {
        const blockedMessage = buildSearchReplyLimitBlockedMessage(status, config);
        statusEl.textContent = blockedMessage
            ? `${blockedMessage}，已自动关闭任务页开关`
            : `近10分钟 ${status?.rolling_10m_count ?? 0}/${status?.config?.rolling_10m_limit ?? config?.rolling_10m_limit ?? '-'}，近1小时 ${status?.rolling_1h_count ?? 0}/${status?.config?.rolling_1h_limit ?? config?.rolling_1h_limit ?? '-'}，近24小时 ${status?.rolling_24h_count ?? 0}/${status?.config?.rolling_24h_limit ?? config?.rolling_24h_limit ?? '-'}`;
    }
    applySearchReplyLimitStatus(status);
}

async function loadLicenseManagementSettings(showSuccessToast = false) {
    try {
        const [crawlRes, sendRes, interactRes, searchReplyRes] = await Promise.all([
            fetchWithTimeout(`${API_BASE}/crawl-limit/config`),
            fetchWithTimeout(`${API_BASE}/send-limit/config`),
            fetchWithTimeout(`${API_BASE}/interact-limit/config`),
            fetchWithTimeout(`${API_BASE}/search-reply-limit/config`),
        ]);
        const crawlData = await crawlRes.json();
        const sendData = await sendRes.json();
        const interactData = await interactRes.json();
        const searchReplyData = await searchReplyRes.json();

        fillLicenseCrawlLimitConfig({
            config: crawlData || {},
            status: {
                daily_limit: crawlData?.daily_limit,
                ...(await (await fetchWithTimeout(`${API_BASE}/crawl-limit/status`)).json()),
            },
        });
        fillLicenseSendLimitConfig(sendData?.data || {});
        fillLicenseInteractLimitConfig(interactData?.data || {});
        fillLicenseSearchReplyLimitConfig(searchReplyData?.data || {});

        if (showSuccessToast) {
            showToast('参数设置已刷新', 'success');
        }
    } catch (e) {
        showToast(`加载参数设置失败: ${e.message}`, 'error');
    }
}

async function openLicenseManagementModal() {
    updateLicenseModalStatus(currentLicenseStatus);
    const modalElement = document.getElementById('licenseModal');
    if (!modalElement || !window.bootstrap) return;
    await Promise.all([loadLicenseManagementSettings(), loadLicenseRequestCode()]);
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
}

function clearLicenseLoginError() {
    const errorEl = document.getElementById('license-login-error');
    if (!errorEl) return;
    errorEl.textContent = '';
    errorEl.classList.add('d-none');
}

function showLicenseLoginError(message) {
    const errorEl = document.getElementById('license-login-error');
    if (!errorEl) return;
    errorEl.textContent = message || '登录失败';
    errorEl.classList.remove('d-none');
}

function showLicenseModal() {
    const modalElement = document.getElementById('licenseLoginModal');
    if (!modalElement || !window.bootstrap) return;
    clearLicenseLoginError();

    const usernameInput = document.getElementById('license-login-username');
    const passwordInput = document.getElementById('license-login-password');
    if (usernameInput) usernameInput.value = '';
    if (passwordInput) passwordInput.value = '';

    const modal = new bootstrap.Modal(modalElement);
    modal.show();
    window.setTimeout(() => usernameInput?.focus(), 150);
}

async function submitLicenseLogin() {
    const usernameInput = document.getElementById('license-login-username');
    const passwordInput = document.getElementById('license-login-password');
    const submitBtn = document.getElementById('license-login-submit-btn');
    const username = usernameInput?.value?.trim() || '';
    const password = passwordInput?.value || '';

    clearLicenseLoginError();

    if (!username || !password) {
        showLicenseLoginError('请输入账户和密码');
        return;
    }

    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = '登录中...';
    }

    try {
        await fetchWithTimeout(`${API_BASE}/license/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });

        const loginModalElement = document.getElementById('licenseLoginModal');
        const loginModal = loginModalElement ? bootstrap.Modal.getInstance(loginModalElement) : null;
        loginModal?.hide();
        await openLicenseManagementModal();
    } catch (e) {
        showLicenseLoginError(e?.message || '登录失败');
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = '登录';
        }
    }
}

function updateDingTalkModalStatus(config) {
    const el = document.getElementById('dingtalk-modal-status');
    if (!el) return;

    if (!config) {
        el.textContent = '未配置';
        return;
    }

    if (!config.enabled) {
        el.textContent = '未启用';
        return;
    }

    if (!config.app_secret_configured || !config.app_key_masked || !config.agent_id) {
        el.textContent = '配置不完整';
        return;
    }

    if (!config.receiver_userid) {
        el.textContent = '未绑定接收人';
        return;
    }

    if (config.last_bind_status === 'failed') {
        el.textContent = `绑定失败：${config.last_bind_error || '请重试'}`;
        return;
    }

    el.textContent = `已绑定 ${config.receiver_name || '固定接收人'} (${config.receiver_userid})`;
}

function fillDingTalkConfigForm(config) {
    currentDingTalkConfig = config || null;
    document.getElementById('dingtalk-enabled').checked = !!config?.enabled;
    document.getElementById('dingtalk-app-key').value = '';
    document.getElementById('dingtalk-app-key').placeholder = config?.app_key_masked || '请输入钉钉 AppKey';
    document.getElementById('dingtalk-app-secret').value = '';
    document.getElementById('dingtalk-agent-id').value = config?.agent_id || '';
    document.getElementById('dingtalk-receiver-name').value = config?.receiver_name || '';
    document.getElementById('dingtalk-receiver-mobile').value = config?.receiver_mobile || '';
    document.getElementById('dingtalk-receiver-userid').value = config?.receiver_userid || '';
    updateDingTalkModalStatus(config);
}

function getDingTalkNotificationStatusMeta(item) {
    const status = item?.dingtalk_notification_status || '';
    const error = item?.dingtalk_notification_error || '';
    if (!status) {
        return null;
    }

    if (status === 'sent') {
        return {
            text: '钉钉提醒：已发送',
            className: 'text-success'
        };
    }

    if (status === 'failed') {
        return {
            text: `钉钉提醒：发送失败${error ? `（${error}）` : ''}`,
            className: 'text-danger'
        };
    }

    if (status === 'skipped') {
        const reasonTextMap = {
            disabled: '未启用钉钉提醒',
            receiver_not_bound: '未绑定接收人',
            already_completed: '该事项已标记完成',
            already_sent: '该提醒已发送过',
            config_incomplete: '钉钉配置不完整'
        };
        return {
            text: `钉钉提醒：未发送（${reasonTextMap[error] || '当前条件未触发'}）`,
            className: 'text-muted'
        };
    }

    return {
        text: '钉钉提醒：未发送',
        className: 'text-muted'
    };
}

async function loadDingTalkConfig() {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/dingtalk/config`);
        const data = await res.json();
        if (!data?.success || !data?.data) {
            throw new Error(data?.message || '加载钉钉配置失败');
        }
        fillDingTalkConfigForm(data.data);
    } catch (e) {
        showToast(`加载钉钉配置失败: ${e.message}`, 'error');
    }
}

function showDingTalkConfigModal() {
    loadDingTalkConfig();
    const modalElement = document.getElementById('dingtalkConfigModal');
    if (!modalElement || !window.bootstrap) return;
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
}

async function saveDingTalkConfig() {
    const payload = {
        enabled: document.getElementById('dingtalk-enabled').checked,
        app_key: document.getElementById('dingtalk-app-key').value.trim(),
        app_secret: document.getElementById('dingtalk-app-secret').value.trim(),
        agent_id: document.getElementById('dingtalk-agent-id').value.trim(),
        receiver_name: document.getElementById('dingtalk-receiver-name').value.trim(),
        receiver_mobile: document.getElementById('dingtalk-receiver-mobile').value.trim(),
        notify_contact_provided_only: true
    };

    try {
        const res = await fetchWithTimeout(`${API_BASE}/dingtalk/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '保存钉钉配置失败');
        }
        if (data?.data) {
            fillDingTalkConfigForm(data.data);
        }
        showToast(data.message || '钉钉配置已保存', 'success');
    } catch (e) {
        showToast(`保存钉钉配置失败: ${e.message}`, 'error');
    }
}

async function bindDingTalkReceiver() {
    const receiverMobile = document.getElementById('dingtalk-receiver-mobile').value.trim();
    if (!receiverMobile) {
        showToast('请先输入接收人手机号', 'warning');
        return;
    }

    try {
        const res = await fetchWithTimeout(`${API_BASE}/dingtalk/bind-receiver`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ receiver_mobile: receiverMobile })
        });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '绑定接收人失败');
        }
        document.getElementById('dingtalk-receiver-userid').value = data?.data?.receiver_userid || '';
        await loadDingTalkConfig();
        showToast(data.message || '接收人绑定成功', 'success');
    } catch (e) {
        showToast(`绑定接收人失败: ${e.message}`, 'error');
    }
}

async function sendDingTalkTestMessage() {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/dingtalk/test-send`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '测试发送失败');
        }
        await loadDingTalkConfig();
        showToast(data.message || '测试消息已提交发送', 'success');
    } catch (e) {
        showToast(`测试发送失败: ${e.message}`, 'error');
    }
}

async function activateLicense() {
    const keyInput = document.getElementById('license-key-input');
    const customerNameInput = document.getElementById('license-customer-name');
    const phoneNumberInput = document.getElementById('license-phone-number');
    const licenseKey = keyInput?.value?.trim();
    const customerName = customerNameInput?.value?.trim() || '';
    const phoneNumber = phoneNumberInput?.value?.trim() || '';

    if (!licenseKey) {
        showToast('请先输入激活码', 'warning');
        return;
    }

    if (!phoneNumber) {
        showToast('请输入注册手机号', 'warning');
        return;
    }

    try {
        const res = await fetchWithTimeout(`${API_BASE}/license/activate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ license_key: licenseKey, phone_number: phoneNumber, customer_name: customerName })
        });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '激活失败');
        }
        showToast(data.message || '激活成功', 'success');
        await refreshLicenseStatus();
    } catch (e) {
        showToast(`激活失败: ${e.message}`, 'error');
    }
}

async function deactivateLicense() {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/license/deactivate`, { method: 'POST' });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '清除授权失败');
        }
        showToast(data.message || '授权已清除', 'info');
        await refreshLicenseStatus();
    } catch (e) {
        showToast(`清除授权失败: ${e.message}`, 'error');
    }
}

async function exportLicenseManagementData() {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/license/export-data`, {
            method: 'POST',
        }, 60000);
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '导出失败');
        }
        const exportDir = data?.data?.export_dir || 'data/export_bundle';
        const counts = data?.data?.counts || {};
        showToast(
            `导出完成：知识库 ${counts.knowledge_items ?? 0} 条，高意向 ${counts.high_intent_customers ?? 0} 条，留资 ${counts.contact_provided_customers ?? 0} 条。目录：${exportDir}`,
            'success'
        );
    } catch (e) {
        showToast(`一键导出失败: ${e.message}`, 'error');
    }
}

async function saveLicenseCrawlLimitConfig() {
    const payload = {
        enabled: document.getElementById('license-crawl-limit-enabled')?.checked ?? false,
        daily_limit: parseInt(document.getElementById('license-crawl-limit-daily')?.value || '0', 10) || 0,
    };
    try {
        const res = await fetchWithTimeout(`${API_BASE}/crawl-limit/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.error || '保存爬取限制失败');
        }
        await loadLicenseManagementSettings();
        showToast('爬取限制已保存', 'success');
    } catch (e) {
        showToast(`保存爬取限制失败: ${e.message}`, 'error');
    }
}

async function saveLicenseSendLimitConfig() {
    const payload = {
        enabled: document.getElementById('license-send-limit-enabled')?.checked ?? false,
        initial_hourly_limit: parseInt(document.getElementById('license-send-initial-hourly')?.value || '0', 10) || 0,
        initial_daily_limit: parseInt(document.getElementById('license-send-initial-daily')?.value || '0', 10) || 0,
        hourly_increment: parseInt(document.getElementById('license-send-hourly-increment')?.value || '0', 10) || 0,
        daily_increment: parseInt(document.getElementById('license-send-daily-increment')?.value || '0', 10) || 0,
        max_hourly_limit: parseInt(document.getElementById('license-send-max-hourly')?.value || '0', 10) || 0,
        max_daily_limit: parseInt(document.getElementById('license-send-max-daily')?.value || '0', 10) || 0,
        increment_cycle_days: parseInt(document.getElementById('license-send-cycle-days')?.value || '0', 10) || 0,
        event_retention_days: parseInt(document.getElementById('license-send-retention-days')?.value || '0', 10) || 0,
    };
    try {
        const res = await fetchWithTimeout(`${API_BASE}/send-limit/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '保存自动私信设置失败');
        }
        fillLicenseSendLimitConfig(data?.data || {});
        showToast('自动私信设置已保存', 'success');
    } catch (e) {
        showToast(`保存自动私信设置失败: ${e.message}`, 'error');
    }
}

async function saveLicenseInteractLimitConfig() {
    const payload = {
        enabled: document.getElementById('license-interact-limit-enabled')?.checked ?? false,
        min_interval_seconds: parseInt(document.getElementById('license-interact-min-interval')?.value || '0', 10) || 0,
        rolling_10m_limit: parseInt(document.getElementById('license-interact-10m-limit')?.value || '0', 10) || 0,
        rolling_24h_limit: parseInt(document.getElementById('license-interact-24h-limit')?.value || '0', 10) || 0,
        event_retention_days: parseInt(document.getElementById('license-interact-retention-days')?.value || '0', 10) || 0,
    };
    try {
        const res = await fetchWithTimeout(`${API_BASE}/interact-limit/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '保存一键互动设置失败');
        }
        fillLicenseInteractLimitConfig(data?.data || {});
        showToast('一键互动设置已保存', 'success');
    } catch (e) {
        showToast(`保存一键互动设置失败: ${e.message}`, 'error');
    }
}

async function refreshRemoteControlStatus(showToastOnSuccess = false) {
    try {
        const endpoint = showToastOnSuccess ? `${API_BASE}/remote-control/refresh` : `${API_BASE}/remote-control/status`;
        const method = showToastOnSuccess ? 'POST' : 'GET';
        const res = await fetchWithTimeout(endpoint, { method });
        const data = await res.json();
        if (data?.success && data?.data) {
            currentRemoteControlStatus = data.data;
            updateRemoteControlBadge(currentRemoteControlStatus);
            if (showToastOnSuccess) {
                const detail = currentRemoteControlStatus.message || currentRemoteControlStatus.reason || '远控策略已刷新';
                showToast(detail, 'success');
            }
            return;
        }
        throw new Error(data?.message || '远控策略刷新失败');
    } catch (e) {
        if (currentRemoteControlStatus) {
            updateRemoteControlBadge(currentRemoteControlStatus);
        }
        if (showToastOnSuccess) {
            showToast(`远控刷新失败: ${e.message}`, 'error');
        }
    }
}

/**
 * 创建Toast容器
 */
function createToastContainer() {
    const container = document.createElement('div');
    container.id = 'toast-container';
    container.style.cssText = 'position: fixed; top: 20px; right: 20px; z-index: 9999; max-width: 400px;';
    document.body.appendChild(container);
    return container;
}

/**
 * 关闭Toast
 */
function closeToast(toastId) {
    const toast = document.getElementById(toastId);
    if (toast) {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }
}

/**
 * 设置按钮加载状态
 */
function setButtonLoading(btn, loading, loadingText = '处理中...') {
    if (!btn) return;
    if (loading) {
        btn.dataset.originalText = btn.textContent;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${loadingText}`;
    } else {
        btn.disabled = false;
        btn.textContent = btn.dataset.originalText || btn.textContent;
    }
}

function formatSummaryTime(value) {
    if (!value) return '不限';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

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

function extractVideoIdFromUrl(url) {
    const text = String(url || '').trim();
    if (!text) return '';

    const pathMatch = text.match(/\/(?:video|note)\/([0-9A-Za-z_-]+)/i);
    if (pathMatch) return pathMatch[1];

    const queryMatch = text.match(/[?&](?:aweme_id|modal_id)=([0-9A-Za-z_-]+)/i);
    return queryMatch ? queryMatch[1] : '';
}

function extractVideoKindFromUrl(url) {
    const text = String(url || '').trim().toLowerCase();
    if (!text) return 'video';
    if (text.includes('/note/')) return 'note';
    return 'video';
}

function getCanonicalVideoUrl(url) {
    const videoId = extractVideoIdFromUrl(url);
    if (!videoId) return '';
    const detailKind = extractVideoKindFromUrl(url);
    return `https://www.douyin.com/${detailKind}/${videoId}`;
}

function getCustomerVideoDisplay(customer = {}) {
    const videoTitle = String(customer.video_title || '').trim();
    const authorName = String(customer.author_name || '').trim();
    const rawSourceVideoUrl = String(customer.first_source_video_url || customer.source_video_url || '').trim();
    const sourceVideoUrl = getCanonicalVideoUrl(rawSourceVideoUrl);
    const videoId = extractVideoIdFromUrl(sourceVideoUrl);
    const hasUrl = Boolean(rawSourceVideoUrl);
    const hasCanonicalUrl = Boolean(sourceVideoUrl);
    // 只在链接本身异常时提示，避免对可正常跳转的视频误报提醒。
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

function getPreferredCustomerVideoUrl(customer = {}) {
    return getCustomerVideoDisplay(customer).sourceVideoUrl || '';
}

async function loadLicenseRequestCode() {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/license/request-code`);
        const data = await res.json();
        if (!data?.success || !data?.data) {
            throw new Error(data?.message || '加载请求码失败');
        }
        const requestCodeEl = document.getElementById('license-request-code');
        const metaEl = document.getElementById('license-request-code-meta');
        if (requestCodeEl) {
            requestCodeEl.value = data.data.request_code || '';
        }
        if (metaEl) {
            const profile = data?.data?.machine_profile || {};
            metaEl.textContent = `当前设备 ${formatMachineHashPreview(data?.data?.machine_hash)}，指纹版本 ${data?.data?.fingerprint_version || '-'}，有效硬件项 ${profile?.available_count ?? 0}/${profile?.minimum_required ?? 0}`;
        }
    } catch (e) {
        const metaEl = document.getElementById('license-request-code-meta');
        if (metaEl) {
            metaEl.textContent = `加载请求码失败：${e.message}`;
        }
    }
}

function copyLicenseRequestCode() {
    const requestCodeEl = document.getElementById('license-request-code');
    const requestCode = requestCodeEl?.value?.trim() || '';
    if (!requestCode) {
        showToast('当前没有可复制的请求码', 'warning');
        return;
    }
    if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(requestCode)
            .then(() => showToast('请求码已复制', 'success'))
            .catch(() => {
                requestCodeEl.select();
                document.execCommand('copy');
                showToast('请求码已复制', 'success');
            });
        return;
    }
    requestCodeEl.select();
    document.execCommand('copy');
    showToast('请求码已复制', 'success');
}

function openCustomerVideoSource(button) {
    const videoUrl = String(button?.dataset?.videoUrl || '').trim();
    const needsWarning = String(button?.dataset?.videoWarning || '').trim() === '1';
    const warningText = String(button?.dataset?.videoWarningText || '').trim();
    if (!videoUrl) return;
    if (needsWarning) {
        const confirmed = window.confirm(warningText || '该来源视频可能已失效，继续打开后可能跳转到抖音推荐流。是否继续？');
        if (!confirmed) return;
    }
    window.open(videoUrl, '_blank', 'noopener,noreferrer');
}

function formatUnixTimestamp(value) {
    if (!value) return '-';
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric <= 0) return '-';
    const parsed = new Date(numeric * 1000);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
}

function formatTimePresetLabel(summary) {
    if (summary?.comment_time_preset_label) return summary.comment_time_preset_label;
    if (summary?.comment_time_preset_days) return `${summary.comment_time_preset_days}天内`;
    if (summary?.comment_time_start || summary?.comment_time_end) {
        return `${formatSummaryTime(summary.comment_time_start)} ~ ${formatSummaryTime(summary.comment_time_end)}`;
    }
    return '不限';
}

function formatAutoReplyFailureReason(reason) {
    const labels = {
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
    return labels[String(reason || '').trim()] || String(reason || '未知失败');
}

function buildAutoReplyProgressDetail(summary) {
    if (!summary?.auto_reply_enabled) return '';

    const attempted = Number(summary.auto_reply_attempted || 0);
    const success = Number(summary.auto_reply_success || 0);
    const failed = Number(summary.auto_reply_failed ?? Math.max(attempted - success, 0));
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
        ?? summary.processed_count
        ?? summary.completed_videos
        ?? 0
    );
    const crawledComments = Number(
        summary.comments_crawled
        ?? summary.total_comments
        ?? (
            Number(summary.top_level_comments || 0)
            + Number(summary.reply_comments || 0)
        )
    );
    return `已抓取视频 ${processedVideos} 个，已抓取评论 ${crawledComments} 条`;
}

function formatCompletionStatusLabel(status) {
    const normalized = String(status || '').trim();
    const labels = {
        completed_full: '完整完成',
        completed_incremental: '增量完成',
        completed_quota_limited: '达到配额后完成',
        partial_due_to_limits: '部分完成',
        partial_due_to_risk: '风控中断',
        stopped_by_user: '手动停止',
        running: '执行中',
    };
    return labels[normalized] || normalized || '未知';
}

function formatTerminationReasonLabel(reason) {
    const normalized = String(reason || '').trim();
    const labels = {
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
    return labels[normalized] || normalized || '未知';
}

function getTimePresetStartIso(daysValue) {
    const days = parseInt(daysValue, 10);
    if (!days || Number.isNaN(days) || days <= 0) return '';
    const dt = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
    return dt.toISOString();
}

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

function buildSearchCommentTimeRange() {
    const rangeValue = document.getElementById('comment_time_range')?.value || '';
    if (!rangeValue) {
        return {
            valid: true,
            comment_time_start: '',
            comment_time_end: ''
        };
    }

    const parts = rangeValue.split(' 至 ');
    let startDate = parts[0] ? new Date(parts[0]) : null;
    let endDate = parts[1] ? new Date(parts[1]) : startDate;

    if (!startDate || Number.isNaN(startDate.getTime())) {
        return {
            valid: true,
            comment_time_start: '',
            comment_time_end: ''
        };
    }
    
    // 如果只有 startDate，endDate 等于 startDate
    if (!endDate || Number.isNaN(endDate.getTime())) {
        endDate = startDate;
    }

    // 确保 endDate 包含当天的最后一秒
    endDate.setHours(23, 59, 59, 999);
    startDate.setHours(0, 0, 0, 0);

    return {
        valid: true,
        comment_time_start: formatDateTimeForApi(startDate),
        comment_time_end: formatDateTimeForApi(endDate)
    };
}

function buildCustomerCommentTimeRange() {
    const rangeValue = document.getElementById('filter-comment-time')?.value || '';
    if (!rangeValue) {
        return {
            valid: true,
            comment_time_start: '',
            comment_time_end: ''
        };
    }

    const parts = rangeValue.split(' 至 ');
    let startDate = parts[0] ? new Date(parts[0]) : null;
    let endDate = parts[1] ? new Date(parts[1]) : startDate;

    if (!startDate || Number.isNaN(startDate.getTime())) {
        return {
            valid: true,
            comment_time_start: '',
            comment_time_end: ''
        };
    }

    if (!endDate || Number.isNaN(endDate.getTime())) {
        endDate = startDate;
    }

    endDate.setHours(23, 59, 59, 999);
    startDate.setHours(0, 0, 0, 0);

    return {
        valid: true,
        comment_time_start: formatDateTimeForApi(startDate),
        comment_time_end: formatDateTimeForApi(endDate)
    };
}

let latestSearchSummary = null;
try {
    const cached = localStorage.getItem('latestSearchSummary');
    if (cached) latestSearchSummary = JSON.parse(cached);
} catch(e) {}

// 状态轮询（带退避机制）
function scheduleNextStatusPoll(delay = pollBackoff.getInterval()) {
    if (pageIsUnloading) return;
    if (statusPollTimer) {
        clearTimeout(statusPollTimer);
    }
    statusPollTimer = setTimeout(pollStatus, delay);
}

async function pollStatus() {
    if (pageIsUnloading) {
        return;
    }

    if (document.visibilityState === 'hidden') {
        scheduleNextStatusPoll(Math.max(pollBackoff.getInterval(), 15000));
        return;
    }

    try {
        const res = await fetchWithTimeout(`${API_BASE}/status`, {}, 45000);
        const data = await res.json();
        pollBackoff.onSuccess();
        const crawlerProcess = data.crawler_process || {};
        const currentTask = data.current_task || 'Idle';
        const crawlerTask = crawlerProcess.current_task || 'Idle';
        const crawlerBusy = Boolean(crawlerProcess.alive) && (Boolean(crawlerProcess.busy) || crawlerTask !== 'Idle');
        const crawlerStalled = Boolean(crawlerProcess.alive) && Boolean(crawlerProcess.stalled);
        if (crawlerStopPending && !crawlerBusy) {
            crawlerStopPending = false;
        }
        const taskKind = deriveCrawlerTaskKind(data);
        const searchTaskRunning = taskKind === 'search';
        const sendTaskRunning = taskKind === 'send';
        const interactTaskRunning = taskKind === 'interact';
        const backgroundBusy = (crawlerBusy || crawlerStopPending) && taskKind !== 'search' && taskKind !== 'send' && taskKind !== 'interact';
        const sharedSubmitting = isCrawlerTaskSubmitting();
        const sharedTaskActive = searchTaskRunning || sendTaskRunning || interactTaskRunning || sharedSubmitting;
        const isSyncTask = currentTask.startsWith('同步');
        
        const loginBadge = document.getElementById('login-status');
        if (isRestarting) {
            if (loginBadge) { loginBadge.textContent = "重启中"; loginBadge.className = "badge bg-warning text-dark status-badge"; }
        } else if (data.is_logged_in) {
            if (loginBadge) { loginBadge.textContent = "已登录"; loginBadge.className = "badge bg-success status-badge"; }
        } else {
            if (loginBadge) { loginBadge.textContent = "未登录"; loginBadge.className = "badge bg-warning text-dark status-badge"; }
        }

        const taskBadge = document.getElementById('task-status');
        if (taskBadge) taskBadge.textContent = crawlerStopPending && crawlerBusy ? '停止中' : currentTask;
        
        if (data.last_search_summary) {
            latestSearchSummary = data.last_search_summary;
            localStorage.setItem('latestSearchSummary', JSON.stringify(latestSearchSummary));
        } else if (!latestSearchSummary) {
            try {
                const cached = localStorage.getItem('latestSearchSummary');
                if (cached) {
                    latestSearchSummary = JSON.parse(cached);
                }
            } catch(e) {}
        }
        
        const isRunning = currentTask !== "Idle";
        
        const progressContainer = document.getElementById('progress-container');
        const progressBar = document.getElementById('task-progress-bar');
        const progressDetail = document.getElementById('progress-detail');

        if (data.progress && data.progress.total > 0) {
            if (progressContainer) progressContainer.classList.remove('d-none');
            const percent = Math.round((data.progress.current / data.progress.total) * 100);
            if (progressBar) { progressBar.style.width = `${percent}%`; progressBar.textContent = `${percent}% (${data.progress.current}/${data.progress.total})`; }
            if (progressDetail) {
                const taskKind = deriveCrawlerTaskKind(data);
                const queueDetail = buildSearchQueueProgressDetail(latestSearchSummary);
                const baseDetail = escapeHtml(data.progress.detail || '');
                const parts = [];
                if (taskKind === 'search') {
                    if (baseDetail) {
                        parts.push(baseDetail);
                    } else if (queueDetail) {
                        parts.push(escapeHtml(queueDetail));
                    } else {
                        parts.push('任务执行中');
                    }
                    if (queueDetail) {
                        parts.push(`<span class="text-muted">${escapeHtml(queueDetail)}</span>`);
                    }
                } else {
                    const autoReplyDetail = buildAutoReplyProgressDetail(latestSearchSummary);
                    if (baseDetail) parts.push(baseDetail);
                    if (queueDetail) parts.push(`<span class="text-muted">${queueDetail}</span>`);
                    if (autoReplyDetail) parts.push(`<span class="text-muted">${autoReplyDetail}</span>`);
                }
                if (crawlerStalled) {
                    parts.push(`<span class="text-danger">${escapeHtml(crawlerProcess.stall_reason || '当前任务疑似卡住，系统将尝试自动恢复')}</span>`);
                }
                progressDetail.innerHTML = parts.join(' | ');
            }
        } else {
            if (!isRunning && progressContainer) progressContainer.classList.add('d-none');
        }

        const searchStartBtn = document.getElementById('btn-search-start');
        const searchStopBtn = document.getElementById('btn-search-stop');
        const sendStartBtn = document.getElementById('btn-send-start');
        const sendStopBtn = document.getElementById('btn-send-stop');

        if (crawlerStalled) {
            if (taskBadge) taskBadge.className = "badge bg-warning text-dark";
        } else if (searchTaskRunning) {
            if (taskBadge) taskBadge.className = "badge bg-danger progress-bar-animated";
        } else {
            if (taskBadge) taskBadge.className = isRunning ? "badge bg-danger progress-bar-animated" : "badge bg-success";
        }
        setCrawlerTaskButtonVisibility(sharedTaskActive);
        syncCrawlerStopButtonsPendingState(crawlerStopPending && crawlerBusy);

        if (searchStartBtn && !searchTaskSubmitting) {
            searchStartBtn.disabled = backgroundBusy || sharedSubmitting;
        }
        if (sendStartBtn && !sendTaskSubmitting) {
            sendStartBtn.disabled = backgroundBusy || sharedSubmitting;
        }
        syncInteractTaskButtons({ taskActive: crawlerBusy || sharedSubmitting });
        
        // 监听按钮应反映真实可用的回复能力，而不是残留 runtime 标记。
        isMonitoring = Boolean(
            Object.prototype.hasOwnProperty.call(data || {}, 'effective_monitoring')
                ? data.effective_monitoring
                : data.is_monitoring_messages
        );
        renderBrowserRuntime({
            browser_context: data.browser_context || null,
            browser_context_id: data.browser_context_id || data.browser_context?.id || '',
            bot_runtime: {
                browser_context: data.browser_context || null,
                browser_user_data_dir: data.browser_user_data_dir || '',
                browser_running: data.is_running,
                is_logged_in: data.is_logged_in,
            },
            crawler_runtime: {
                stalled: Boolean(crawlerProcess.stalled),
                stall_reason: crawlerProcess.stall_reason || '',
                stall_duration_seconds: Number(crawlerProcess.stall_duration_seconds || 0),
                force_reset_recommended: Boolean(crawlerProcess.force_reset_recommended),
                login_transfer_matched: Object.prototype.hasOwnProperty.call(crawlerProcess, 'login_transfer_matched')
                    ? Boolean(crawlerProcess.login_transfer_matched)
                    : false,
                browser_ready: Boolean(crawlerProcess.browser_ready),
            },
        });
        updateMonitorStatus(isMonitoring, data.unread_message_count || 0);
        applySearchReplyLimitStatus(data.search_reply_limit || {});

        await maybeAutoRefreshCurrentTaskCustomers(data);

        lastObservedTask = currentTask;
    } catch (e) {
        if (e.name === 'AbortError') {
            console.warn("Status check timeout, will retry");
        } else {
            console.error("Status check failed", e);
        }
        pollBackoff.onError();
    }
    scheduleNextStatusPoll();
}
document.addEventListener('visibilitychange', () => {
    if (!pageIsUnloading && document.visibilityState === 'visible') {
        scheduleNextStatusPoll(500);
    }
});
scheduleNextStatusPoll(0);

/**
 * 启动消息回复
 */
async function startMonitor() {
    const btn = document.getElementById('btn-start-monitor');
    setButtonLoading(btn, true, '启动中...');

    try {
        const res = await fetchWithTimeout(`${API_BASE}/monitor/start`, { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            showToast(data.message || '消息回复已启动', 'success');
            if (data.monitoring_started) {
                await waitForStatusCondition(
                    (status) => deriveStatusFlags(status).isMonitoring,
                    { timeoutMs: 12000, intervalMs: 500 }
                );
            } else {
                await pollStatus();
            }
        } else {
            showToast(data.message || '启动消息回复失败', 'error');
            await pollStatus();
        }
    } catch (e) {
        showToast(getRequestErrorMessage(e, '启动消息回复失败'), 'error');
        await pollStatus();
    } finally {
        setButtonLoading(btn, false);
    }
}

/**
 * 停止消息回复
 */
async function stopMonitor() {
    const btn = document.getElementById('btn-stop-monitor');
    setButtonLoading(btn, true, '停止中...');

    try {
        const res = await fetchWithTimeout(`${API_BASE}/monitor/stop`, { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            showToast(data.message || '消息回复已停止', 'info');
            await waitForStatusCondition(
                (status) => !deriveStatusFlags(status).isMonitoring,
                { timeoutMs: 12000, intervalMs: 500 }
            );
        } else {
            showToast(data.message || '停止消息回复失败', 'error');
            await pollStatus();
        }
    } catch (e) {
        showToast(getRequestErrorMessage(e, '停止消息回复失败'), 'error');
    } finally {
        setButtonLoading(btn, false);
    }
}

/**
 * 停止当前任务
 */
async function stopTask() {
    const searchStopBtn = document.getElementById('btn-search-stop');
    const sendStopBtn = document.getElementById('btn-send-stop');
    const interactStopBtn = document.getElementById('interact-stop-btn');
    crawlerStopPending = true;
    syncCrawlerStopButtonsPendingState(true);
    try {
        const res = await fetchWithTimeout(`${API_BASE}/stop`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message, 'info');
            const stopPending = Boolean(data.pending);
            const timeoutMs = stopPending ? 20000 : 12000;
            const latestStatus = await waitForStatusCondition(
                (status) => !deriveStatusFlags(status).crawlerBusy,
                { timeoutMs, intervalMs: 500 }
            );
            if (stopPending && latestStatus && deriveStatusFlags(latestStatus).crawlerBusy) {
                if (deriveStatusFlags(latestStatus).crawlerStalled) {
                    showToast('当前任务疑似卡住，系统正在尝试强制恢复独立爬取进程', 'warning');
                } else {
                    showToast('停止请求已发送，正在等待当前页面步骤安全结束', 'info');
                }
            }
            if (!latestStatus || !deriveStatusFlags(latestStatus).crawlerBusy) {
                crawlerStopPending = false;
            }
        } else {
            showToast(data.message || '停止任务失败', 'error');
            crawlerStopPending = false;
            await pollStatus();
        }
    } catch (e) {
        showToast(getRequestErrorMessage(e, '停止任务失败'), 'error');
        crawlerStopPending = false;
    } finally {
        syncCrawlerStopButtonsPendingState(crawlerStopPending);
    }
}

/**
 * 启动浏览器
 * 
 * 修复：原实现在API返回后立即显示"启动成功"并重新启用按钮，
 * 但浏览器实际尚未启动完成（API仅提交任务到Worker队列）。
 * 这导致：1)用户误以为启动完成 2)按钮可被重复点击提交多个启动任务
 * 现改为：API返回后显示"启动中"，保持按钮禁用，轮询状态确认启动完成
 */
window.startBrowser = async function() {
    const btn = document.querySelector('[onclick="startBrowser()"]');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span> 启动中...';
    }
    
    try {
        const res = await fetchWithTimeout(`${API_BASE}/start_browser`, { method: 'POST' }, SEARCH_START_TIMEOUT_MS);
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            await pollStatus();
            const ready = isCrawlerBrowserReady(data?.crawler_process) || isCrawlerBrowserReady((await fetchStatusSnapshot())?.crawler_process);
            showToast(data.message || (ready ? '浏览器启动成功' : '浏览器启动中，请稍候...'), ready ? 'success' : 'info');
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '🚀 启动抖音';
            }
        } else {
            showToast(data.message || '启动失败', 'error');
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '🚀 启动抖音';
            }
        }
    } catch (e) {
        showToast(getRequestErrorMessage(e, '启动浏览器失败'), 'error');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '🚀 启动抖音';
        }
    }
}

/**
 * 重启浏览器
 */
async function restartBrowser() {
    if (isRestarting) {
        showToast('浏览器正在重启中，请稍候...', 'warning');
        return;
    }
    if (!confirm("确定要重启浏览器吗？重启期间将暂停所有功能。")) return;

    const btn = document.querySelector('[onclick="restartBrowser()"]');
    setButtonLoading(btn, true, '重启中...');
    isRestarting = true;

    try {
        const res = await fetchWithTimeout(`${API_BASE}/restart_browser`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            showToast('浏览器正在重启，请等待...', 'info');
            setTimeout(() => {
                isRestarting = false;
                setButtonLoading(btn, false);
                showToast('浏览器重启完成', 'success');
            }, 15000);
        } else {
            showToast(data.message || '重启失败', 'error');
            isRestarting = false;
            setButtonLoading(btn, false);
        }
    } catch (e) {
        showToast(getRequestErrorMessage(e, '重启浏览器失败'), 'error');
        isRestarting = false;
        setButtonLoading(btn, false);
    }
}

/**
 * 提交搜索任务
 */
async function startSearchTask(options = {}) {
    if (isCrawlerTaskSubmitting()) {
        return;
    }

    const keyword = document.getElementById('keyword')?.value?.trim() || '';
    const lead_quota = parseInt(document.getElementById('lead_quota')?.value, 10) || 5;
    const comment_keywords = document.getElementById('comment_keywords')?.value?.trim() || '';
    const auto_reply_enabled = Boolean(document.getElementById('enable_search_reply')?.checked);
    const reply_quota = parseInt(document.getElementById('reply_quota')?.value, 10) || 10;
    const reply_templates = Array.from(document.querySelectorAll('.search-reply-input'))
        .map((input) => input.value?.trim() || '')
        .filter(Boolean);
    const skip_crawled = Boolean(document.getElementById('skip_crawled')?.checked);
    const skip_existing_videos = Boolean(document.getElementById('skip_existing_videos')?.checked);
    const crawl_priority = document.getElementById('crawl_priority')?.value || 'latest_unprocessed';
    const worker_threads = parseInt(document.getElementById('worker_threads')?.value, 10) || 1;
    const platform = document.getElementById('platform')?.value || 'douyin';
    const timeRange = buildSearchCommentTimeRange();

    if (!keyword) {
        showToast('请输入搜索关键词', 'warning');
        return;
    }

    if (auto_reply_enabled && reply_templates.length === 0) {
        showToast('已开启评论下直接回复，请至少填写一条回复内容', 'warning');
        return;
    }

    try {
        const limitRes = await fetchWithTimeout(`${API_BASE}/crawl-limit/status`);
        if (limitRes.ok) {
            const limitData = await limitRes.json();
            if (limitData.limited) {
                const limitModalHtml = `
                    <div class="modal fade" id="crawlLimitModal" tabindex="-1">
                        <div class="modal-dialog modal-dialog-centered">
                            <div class="modal-content">
                                <div class="modal-header bg-warning">
                                    <h5 class="modal-title">⚠️ 爬取额度提醒</h5>
                                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                                </div>
                                <div class="modal-body text-center py-4">
                                    <h4 class="mb-3">今日爬取扫描量已达上限</h4>
                                    <p class="fs-5 text-danger mb-2">(${limitData.today_scanned} / ${limitData.daily_limit})</p>
                                    <p class="text-muted">为保护您的账号风控安全，请明日再试。<br>（后台管理员可通过修改配置文件提升上限）</p>
                                </div>
                                <div class="modal-footer justify-content-center">
                                    <button type="button" class="btn btn-secondary px-4" data-bs-dismiss="modal">我知道了</button>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
                const oldModal = document.getElementById('crawlLimitModal');
                if (oldModal) oldModal.remove();
                document.body.insertAdjacentHTML('beforeend', limitModalHtml);
                
                const modalElement = document.getElementById('crawlLimitModal');
                bindModalAccessibility(modalElement, { disposeOnHidden: true });
                const modal = new window.bootstrap.Modal(modalElement);
                modal.show();
                return;
            }
        }
    } catch (e) {
        console.warn("获取爬取限制状态失败，继续执行", e);
    }

    const btn = document.getElementById('btn-search-start');
    searchTaskSubmitting = true;
    setButtonLoading(btn, true, '启动中...');
    applyCrawlerStartSharedCooldown('search');
    setCrawlerTaskButtonVisibility(true);

    try {
        const res = await fetchWithTimeout(`${API_BASE}/search`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                keyword,
                lead_quota,
                comment_keywords,
                auto_reply_enabled,
                reply_quota,
                reply_templates,
                platform,
                comment_time_start: timeRange.comment_time_start,
                comment_time_end: timeRange.comment_time_end,
                skip_crawled,
                skip_existing_videos,
                crawl_priority,
                worker_threads,
            })
        }, SEARCH_START_TIMEOUT_MS);
        const data = await res.json();
        if (res.ok) {
            showToast(data.message, 'success');
            await waitForStatusCondition(
                (status) => deriveStatusFlags(status).crawlerBusy,
                { timeoutMs: 12000, intervalMs: 500 }
            );
        } else {
            showToast(data.message || '搜索任务启动失败', 'error');
            await pollStatus();
        }
    } catch (e) {
        showToast(getRequestErrorMessage(e, '搜索任务启动失败'), 'error');
        await pollStatus();
    } finally {
        searchTaskSubmitting = false;
        setButtonLoading(btn, false);
    }
}

window.startSearchTask = startSearchTask;

function deriveStatusFlags(data) {
    const crawlerProcess = data?.crawler_process || {};
    const crawlerTask = crawlerProcess.current_task || 'Idle';
    const crawlerBusy = Boolean(crawlerProcess.alive) && (Boolean(crawlerProcess.busy) || crawlerTask !== 'Idle');
    const crawlerStalled = Boolean(crawlerProcess.alive) && Boolean(crawlerProcess.stalled);
    const isMonitoring = Boolean(
        Object.prototype.hasOwnProperty.call(data || {}, 'effective_monitoring')
            ? data.effective_monitoring
            : data?.is_monitoring_messages
    );
    return { crawlerBusy, crawlerStalled, isMonitoring };
}

function isCrawlerBrowserReady(crawlerProcess = {}) {
    return Boolean(
        crawlerProcess
        && crawlerProcess.alive
        && crawlerProcess.browser_ready
        && crawlerProcess.initialized
    );
}

function deriveCrawlerTaskKind(data) {
    const crawlerProcess = data?.crawler_process || {};
    const crawlerTask = String(crawlerProcess.current_task || 'Idle');
    if (crawlerTask.startsWith('Searching:') || crawlerTask.startsWith('Crawling video ')) {
        return 'search';
    }
    if (crawlerTask === 'Sending messages') {
        return 'send';
    }
    if (crawlerTask.startsWith('Interacting:')) {
        return 'interact';
    }
    if (crawlerTask === 'Initializing browser') {
        return 'browser_init';
    }
    const crawlerBusy = Boolean(crawlerProcess.alive) && (Boolean(crawlerProcess.busy) || crawlerTask !== 'Idle');
    return crawlerBusy ? 'busy' : 'idle';
}

async function fetchStatusSnapshot() {
    const res = await fetchWithTimeout(`${API_BASE}/status`, {}, 45000);
    return await res.json();
}

async function waitForStatusCondition(predicate, { timeoutMs = 10000, intervalMs = 500 } = {}) {
    const deadline = Date.now() + timeoutMs;
    let lastStatus = null;
    while (Date.now() < deadline) {
        try {
            lastStatus = await fetchStatusSnapshot();
            if (predicate(lastStatus)) {
                break;
            }
        } catch (e) {
            console.debug('waitForStatusCondition polling failed:', e);
        }
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    await pollStatus();
    return lastStatus;
}

function initSearchForm() {
    const searchForm = document.getElementById('search-form');
    if (!searchForm) return;

    // 初始化 flatpickr 日期范围选择器
    const timeRangeInput = document.getElementById('comment_time_range');
    if (timeRangeInput && typeof flatpickr !== 'undefined') {
        flatpickr(timeRangeInput, {
            mode: "range",
            maxDate: "today",
            locale: "zh",
            dateFormat: "Y-m-d",
            altInput: true,
            altFormat: "Y-m-d",
            allowInput: true,
            placeholder: "不限制时间，可点击选择范围"
        });
    }

    initSearchReplyForm();

    searchForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        await startSearchTask();
    });
}

function saveSearchRepliesToLocal() {
    const inputs = document.querySelectorAll('.search-reply-input');
    const replies = Array.from(inputs).map(input => input.value);
    localStorage.setItem('savedSearchReplyTemplates', JSON.stringify(replies));
}

function toggleSearchReplyConfig() {
    const enabled = Boolean(document.getElementById('enable_search_reply')?.checked);
    const config = document.getElementById('search-reply-config');
    if (config) {
        config.classList.toggle('d-none', !enabled);
    }
}

async function saveLicenseSearchReplyLimitConfig() {
    const payload = {
        enabled: document.getElementById('license-search-reply-limit-enabled')?.checked ?? false,
        rolling_10m_limit: parseInt(document.getElementById('license-search-reply-10m-limit')?.value || '0', 10) || 0,
        rolling_1h_limit: parseInt(document.getElementById('license-search-reply-1h-limit')?.value || '0', 10) || 0,
        rolling_24h_limit: parseInt(document.getElementById('license-search-reply-24h-limit')?.value || '0', 10) || 0,
        event_retention_days: parseInt(document.getElementById('license-search-reply-retention-days')?.value || '0', 10) || 0,
    };
    try {
        const res = await fetchWithTimeout(`${API_BASE}/search-reply-limit/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!data?.success) {
            throw new Error(data?.message || '保存评论下回复设置失败');
        }
        fillLicenseSearchReplyLimitConfig(data?.data || {});
        showToast('评论下回复设置已保存', 'success');
    } catch (e) {
        showToast(`保存评论下回复设置失败: ${e.message}`, 'error');
    }
}

function addSearchReplyInput(value = '', save = true) {
    const container = document.getElementById('search-reply-inputs-container');
    if (!container) return;

    const div = document.createElement('div');
    div.className = 'input-group input-group-sm';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control search-reply-input';
    input.placeholder = '请输入评论下直接回复内容...';
    input.value = value;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline-danger';
    btn.textContent = '删除';
    btn.onclick = () => removeSearchReplyInput(btn);

    div.appendChild(input);
    div.appendChild(btn);
    container.appendChild(div);

    if (save) {
        saveSearchRepliesToLocal();
    }
}

function removeSearchReplyInput(btn) {
    const container = document.getElementById('search-reply-inputs-container');
    if (!container) return;
    if (container.children.length <= 1) {
        showToast('至少保留一条评论下回复模板', 'warning');
        return;
    }
    btn.closest('.input-group')?.remove();
    saveSearchRepliesToLocal();
}

function initSearchReplyForm() {
    const container = document.getElementById('search-reply-inputs-container');
    const toggle = document.getElementById('enable_search_reply');
    if (!container || !toggle) return;

    if (container.children.length === 0) {
        try {
            const saved = localStorage.getItem('savedSearchReplyTemplates') || localStorage.getItem('savedSearchCommentTemplates');
            if (saved) {
                const replies = JSON.parse(saved);
                if (Array.isArray(replies) && replies.length > 0) {
                    replies.forEach(reply => addSearchReplyInput(String(reply || ''), false));
                } else {
                    addSearchReplyInput('', false);
                }
            } else {
                addSearchReplyInput('', false);
            }
        } catch (e) {
            addSearchReplyInput('', false);
        }
    }

    toggleSearchReplyConfig();
    toggle.addEventListener('change', toggleSearchReplyConfig);
    fetchWithTimeout(`${API_BASE}/search-reply-limit/status`)
        .then(res => res.json())
        .then(data => {
            if (data?.success && data?.data) {
                applySearchReplyLimitStatus(data.data);
            }
        })
        .catch(() => {});
    container.addEventListener('input', (e) => {
        if (e.target.classList.contains('search-reply-input')) {
            saveSearchRepliesToLocal();
        }
    });
}

window.addSearchReplyInput = addSearchReplyInput;
window.removeSearchReplyInput = removeSearchReplyInput;
window.saveLicenseSearchReplyLimitConfig = saveLicenseSearchReplyLimitConfig;

/**
 * 保存私信内容到本地存储
 */
function saveMessagesToLocal() {
    const inputs = document.querySelectorAll('.message-input');
    const messages = Array.from(inputs).map(input => input.value);
    localStorage.setItem('savedPrivateMessages', JSON.stringify(messages));
}

/**
 * 添加私信输入框
 */
function addMessageInput(value = '', save = true) {
    const container = document.getElementById('message-inputs-container');
    if (!container) return;

    const div = document.createElement('div');
    div.className = 'input-group input-group-sm';
    div.innerHTML = `
        <input type="text" class="form-control message-input" value="${value}" placeholder="请输入私信内容..." required>
        <button class="btn btn-outline-danger" type="button" onclick="removeMessageInput(this)">删除</button>
    `;
    container.appendChild(div);
    
    if (save) {
        saveMessagesToLocal();
    }
}

/**
 * 删除私信输入框
 */
function removeMessageInput(btn) {
    const container = document.getElementById('message-inputs-container');
    if (container.children.length <= 1) {
        showToast('至少保留一条私信内容', 'warning');
        return;
    }
    btn.closest('.input-group').remove();
    saveMessagesToLocal();
}

/**
 * 提交私信任务
 */
// 初始化提交表单相关逻辑
function initSendForm() {
    const sendForm = document.getElementById('send-form');
    if (!sendForm) return;

    const container = document.getElementById('message-inputs-container');
    if (container) {
        // 初始化恢复私信
        if (container.children.length === 0) {
            try {
                const saved = localStorage.getItem('savedPrivateMessages');
                if (saved) {
                    const messages = JSON.parse(saved);
                    if (Array.isArray(messages) && messages.length > 0) {
                        messages.forEach(msg => addMessageInput(msg, false));
                    } else {
                        addMessageInput('您好！看了您的视频，对我们的产品很感兴趣，想了解一下，麻烦通过一下~', false);
                    }
                } else {
                    addMessageInput('您好！看了您的视频，对我们的产品很感兴趣，想了解一下，麻烦通过一下~', false);
                }
            } catch(e) {
                addMessageInput('您好！看了您的视频，对我们的产品很感兴趣，想了解一下，麻烦通过一下~', false);
            }
        }
        
        // 监听输入框变化实时保存
        container.addEventListener('input', (e) => {
            if (e.target.classList.contains('message-input')) {
                saveMessagesToLocal();
            }
        });
    }

    sendForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        await submitSendTask();
    });
}

async function submitSendTask(options = {}) {
    if (isCrawlerTaskSubmitting()) {
        return;
    }

    const inputs = document.querySelectorAll('.message-input');
    const messages = Array.from(inputs)
        .map(input => input.value.trim())
        .filter(val => val.length > 0);

    const count = parseInt(document.getElementById('send_count').value, 10) || 10;

    if (messages.length === 0) {
        showToast('请至少输入一条私信内容', 'warning');
        return;
    }

    try {
        await ensureBrowserReadyForAction('启动私信任务');
    } catch (e) {
        showToast(getRequestErrorMessage(e, '私信任务启动前运行态校验失败'), 'warning');
        return;
    }

    const btn = document.getElementById('btn-send-start');
    sendTaskSubmitting = true;
    setButtonLoading(btn, true, '发送中...');
    applyCrawlerStartSharedCooldown('send');
    setCrawlerTaskButtonVisibility(true);

    try {
        const res = await fetchWithTimeout(`${API_BASE}/send`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                message: messages[0],
                messages,
                count,
            })
        });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message, 'success');
            await waitForStatusCondition(
                (status) => deriveStatusFlags(status).crawlerBusy,
                { timeoutMs: 12000, intervalMs: 500 }
            );
        } else {
            showToast(data.message || '私信任务启动失败', 'error');
            await pollStatus();
        }
    } catch (e) {
        showToast(getRequestErrorMessage(e, '私信任务启动失败'), 'error');
        await pollStatus();
    } finally {
        sendTaskSubmitting = false;
        setButtonLoading(btn, false);
    }
}

/**
 * 保存一键互动评论模板到本地存储
 */
function saveInteractCommentsToLocal() {
    const inputs = document.querySelectorAll('.interact-comment-input');
    const comments = Array.from(inputs).map(input => input.value);
    localStorage.setItem('savedInteractComments', JSON.stringify(comments));
}

/**
 * 添加一键互动评论输入框
 */
function addInteractCommentInput(value = '', save = true) {
    const container = document.getElementById('interact-comment-inputs-container');
    if (!container) return;

    const div = document.createElement('div');
    div.className = 'input-group input-group-sm';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control interact-comment-input';
    input.placeholder = '请输入评论内容...';
    input.value = value;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline-danger';
    btn.textContent = '删除';
    btn.onclick = () => removeInteractCommentInput(btn);

    div.appendChild(input);
    div.appendChild(btn);
    container.appendChild(div);

    if (save) {
        saveInteractCommentsToLocal();
    }
}

/**
 * 删除一键互动评论输入框
 */
function removeInteractCommentInput(btn) {
    const container = document.getElementById('interact-comment-inputs-container');
    if (!container) return;
    if (container.children.length <= 1) {
        showToast('至少保留一条评论模板', 'warning');
        return;
    }
    btn.closest('.input-group')?.remove();
    saveInteractCommentsToLocal();
}

/**
 * 初始化一键互动表单
 */
function initInteractForm() {
    const container = document.getElementById('interact-comment-inputs-container');
    if (!container) return;

    if (container.children.length === 0) {
        try {
            const saved = localStorage.getItem('savedInteractComments');
            if (saved) {
                const comments = JSON.parse(saved);
                if (Array.isArray(comments) && comments.length > 0) {
                    comments.forEach(comment => addInteractCommentInput(String(comment || ''), false));
                } else {
                    addInteractCommentInput('', false);
                }
            } else {
                addInteractCommentInput('', false);
            }
        } catch (e) {
            addInteractCommentInput('', false);
        }
    }

    container.addEventListener('input', (e) => {
        if (e.target.classList.contains('interact-comment-input')) {
            saveInteractCommentsToLocal();
        }
    });
}

const customerPageState = {
    page: 1,
    pageSize: 20,
    total: 0,
    totalPages: 1,
    query: ''
};
let currentCustomerRows = [];
const selectedCustomerMap = new Map();
const customerAutoRefreshState = {
    loading: false,
    lastRefreshAt: 0,
    lastRefreshKey: '',
};

function getCustomerSelectionKey(customer = {}) {
    const platform = String(customer.platform || 'douyin').trim();
    const secUid = String(customer.sec_uid || '').trim();
    return `${platform}::${secUid}`;
}

function isCustomerSelectable(customer = {}) {
    return Boolean(String(customer.sec_uid || '').trim());
}

function isCustomerSelected(customer = {}) {
    if (!isCustomerSelectable(customer)) return false;
    return selectedCustomerMap.has(getCustomerSelectionKey(customer));
}

function setCustomerSelection(customer = {}, checked = false) {
    if (!isCustomerSelectable(customer)) return;
    const key = getCustomerSelectionKey(customer);
    if (checked) {
        selectedCustomerMap.set(key, {
            sec_uid: String(customer.sec_uid || '').trim(),
            platform: String(customer.platform || 'douyin').trim() || 'douyin',
            nickname: String(customer.nickname || customer.sec_uid || '').trim(),
        });
    } else {
        selectedCustomerMap.delete(key);
    }
}

function syncCustomerSelectionUI() {
    const selectedCount = selectedCustomerMap.size;
    const summaryEl = document.getElementById('customer-selected-summary');
    const exportBtn = document.getElementById('customer-export-selected-btn');
    const clearBtn = document.getElementById('customer-clear-selection-btn');
    const interactBtn = document.getElementById('customer-interact-btn');
    const selectAllEl = document.getElementById('customer-select-all');
    const selectableRows = currentCustomerRows.filter(isCustomerSelectable);
    const selectedOnPage = selectableRows.filter(isCustomerSelected).length;

    if (summaryEl) {
        summaryEl.textContent = `已选 ${selectedCount} 项`;
    }
    if (exportBtn) {
        exportBtn.disabled = selectedCount === 0;
    }
    if (clearBtn) {
        clearBtn.disabled = selectedCount === 0;
    }
    if (interactBtn) {
        interactBtn.disabled = selectedCount === 0;
    }
    if (selectAllEl) {
        selectAllEl.checked = selectableRows.length > 0 && selectedOnPage === selectableRows.length;
        selectAllEl.indeterminate = selectedOnPage > 0 && selectedOnPage < selectableRows.length;
        selectAllEl.disabled = selectableRows.length === 0;
    }
}

function toggleCustomerSelection(customer, checked) {
    setCustomerSelection(customer, checked);
    syncCustomerSelectionUI();
}

function toggleSelectAllCustomers(checked) {
    currentCustomerRows.forEach(customer => {
        if (isCustomerSelectable(customer)) {
            setCustomerSelection(customer, checked);
        }
    });
    document.querySelectorAll('.customer-select-checkbox').forEach(checkbox => {
        checkbox.checked = checked;
    });
    syncCustomerSelectionUI();
}

async function clearCustomerSelection() {
    const items = Array.from(selectedCustomerMap.values()).map(item => ({
        sec_uid: item.sec_uid,
        platform: item.platform || 'douyin',
    }));
    if (!items.length) {
        showToast('请先勾选要删除的客户', 'warning');
        return;
    }

    const confirmed = confirm(`确定要删除选中的 ${items.length} 位客户吗？此操作不可恢复。`);
    if (!confirmed) {
        return;
    }

    const clearBtn = document.getElementById('customer-clear-selection-btn');
    setButtonLoading(clearBtn, true, '删除中...');
    try {
        const { ok, data } = await fetchJson(
            `${API_BASE}/customers/delete-selected`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items }),
            },
            60000
        );
        if (!ok || !data?.success) {
            throw new Error(data?.message || data?.detail || '删除失败');
        }

        selectedCustomerMap.clear();
        await loadCustomers(customerPageState.page);
        showToast(data.message || `成功删除 ${items.length} 位客户`, 'success');
    } catch (e) {
        showToast(`删除选中客户失败: ${e.message}`, 'error');
    } finally {
        setButtonLoading(clearBtn, false);
    }
}

/**
 * 打开一键互动模态框
 */
function openInteractModal() {
    const selectedCount = selectedCustomerMap.size;
    if (selectedCount === 0) {
        showToast('请先勾选要互动的客户', 'warning');
        return;
    }
    
    document.getElementById('interact-selected-count').textContent = `${selectedCount} 位客户`;
    
    const modalEl = document.getElementById('interactModal');
    const modal = new bootstrap.Modal(modalEl);
    modal.show();
}

/**
 * 提交一键互动任务
 */
async function submitInteractTask(options = {}) {
    if (isCrawlerTaskSubmitting()) {
        return;
    }

    const inputs = document.querySelectorAll('.interact-comment-input');
    const comments = Array.from(inputs)
        .map(input => input.value.trim())
        .filter(val => val.length > 0);
    if (comments.length === 0) {
        showToast('请至少输入一条评论内容', 'warning');
        return;
    }
    
    const uids = Array.from(selectedCustomerMap.values()).map(c => c.sec_uid);
    if (uids.length === 0) {
        showToast('请先选择客户', 'warning');
        return;
    }

    try {
        await ensureBrowserReadyForAction('启动一键互动');
    } catch (e) {
        showToast(getRequestErrorMessage(e, '互动任务启动前运行态校验失败'), 'warning');
        return;
    }
    
    const submitBtn = document.getElementById('interact-submit-btn');
    interactTaskSubmitting = true;
    setButtonLoading(submitBtn, true, '正在提交...');
    applyCrawlerStartSharedCooldown('interact');
    
    try {
        const { ok, data } = await fetchJson(
            `${API_BASE}/interact`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    uids,
                    content: comments[0],
                    comments,
                }),
            }
        );
        
        if (!ok) {
            throw new Error(data?.message || data?.detail || '提交任务失败');
        }
        
        showToast('互动任务已启动，请在控制台查看进度', 'success');
        await waitForStatusCondition(
            (status) => deriveStatusFlags(status).crawlerBusy,
            { timeoutMs: 12000, intervalMs: 500 }
        );
        
        // 关闭模态框
        const modalEl = document.getElementById('interactModal');
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
        
        // 自动切换到日志/控制台面板（如果有的话，或者保持当前页）
        // 实际上用户可以看到状态栏的更新
    } catch (e) {
        console.error('Submit interact task failed', e);
        showToast(`提交失败: ${e.message}`, 'error');
        await pollStatus();
    } finally {
        interactTaskSubmitting = false;
        setButtonLoading(submitBtn, false);
        syncInteractTaskButtons({ taskActive: lastObservedTask !== 'Idle' });
    }
}


// 加载客户列表
async function loadCustomers(page = customerPageState.page, options = {}) {
    const { silent = false } = options;
    customerAutoRefreshState.loading = true;
    try {
        const query = document.getElementById('customer-search')?.value?.trim() || '';
        const customerTimeRange = buildCustomerCommentTimeRange();
        const currentTaskOnly = Boolean(document.getElementById('filter-current-task-only')?.checked);
        const platform = document.getElementById('filter-platform')?.value || '';
        const status = document.getElementById('filter-status')?.value || '';
        const interactStatus = document.getElementById('filter-interact-status')?.value || '';
        const pageSize = parseInt(document.getElementById('customer-page-size')?.value || '20', 10);
        const filterHint = document.getElementById('customer-filter-hint');
        const params = new URLSearchParams({
            page: String(Math.max(page, 1)),
            page_size: String(Math.max(pageSize, 1))
        });

        if (platform) params.set('platform', platform);
        if (status) params.set('status', status);
        if (interactStatus) params.set('interact_status', interactStatus);
        if (query) params.set('q', query);
        if (customerTimeRange.comment_time_start) params.set('comment_time_start', customerTimeRange.comment_time_start);
        if (customerTimeRange.comment_time_end) params.set('comment_time_end', customerTimeRange.comment_time_end);
        if (currentTaskOnly) {
            params.set('created_by_task_id', latestSearchSummary?.task_id || '__no_current_task__');
        }

        const endpoint = query ? `${API_BASE}/customers/search` : `${API_BASE}/customers`;
        const res = await fetchWithTimeout(`${endpoint}?${params.toString()}`);
        const data = await res.json();
        const customers = Array.isArray(data) ? data : (data.items || []);
        currentCustomerRows = customers;
        const tbody = document.getElementById('customer-list');
        tbody.innerHTML = '';

        customerPageState.page = data.page || 1;
        customerPageState.pageSize = data.page_size || pageSize;
        customerPageState.total = data.total || customers.length;
        customerPageState.totalPages = Math.max(data.total_pages || 1, 1);
        customerPageState.query = query;
        if (filterHint) {
            const hintParts = [];
            if (customerTimeRange.comment_time_start || customerTimeRange.comment_time_end) {
                hintParts.push(
                    `设置时间范围 ${formatSummaryTime(customerTimeRange.comment_time_start)} ~ ${formatSummaryTime(customerTimeRange.comment_time_end)}`
                );
            }
            if (status) {
                hintParts.push(`私信状态 ${getStatusLabel(status)}`);
            }
            if (interactStatus) {
                hintParts.push(`互动状态 ${getInteractStatusLabel(interactStatus)}`);
            }
            if (currentTaskOnly) {
                hintParts.push(latestSearchSummary?.task_id ? '仅看最近一次爬取新增' : '仅看本次新增：当前暂无最近一次爬取任务');
            }
            filterHint.textContent = hintParts.length
                ? `当前筛选：${hintParts.join(' | ')}`
                : '默认显示全部评论时间，可设置时间范围或仅查看最近一次爬取任务新增的客户。';
        }
        
        customers.forEach(c => {
            const tr = document.createElement('tr');
            const selectable = isCustomerSelectable(c);
            const platformTag = `<span class="platform-tag platform-${escapeHtml(c.platform || 'douyin')}">${escapeHtml(c.platform || '抖音')}</span>`;
            const intentBadge = c.intent_level ? `<span class="badge intent-${escapeHtml(c.intent_level)}">${escapeHtml(c.intent_level)}级</span>` : '-';
            const commentContent = c.comment_content || '';
            const commentPreview = commentContent.length > 20 ? `${commentContent.substring(0, 20)}...` : commentContent;
            const ipLocation = String(c.ip_location || '').trim();
            const videoDisplay = getCustomerVideoDisplay(c);
            const videoPreview = videoDisplay.title.length > 24 ? `${videoDisplay.title.substring(0, 24)}...` : videoDisplay.title;
            
            tr.innerHTML = `
                <td>
                    <input
                        type="checkbox"
                        class="form-check-input customer-select-checkbox"
                        ${selectable ? '' : 'disabled'}
                        ${isCustomerSelected(c) ? 'checked' : ''}
                        aria-label="选择客户 ${escapeHtml(c.nickname || c.sec_uid || '')}"
                    >
                </td>
                <td>${platformTag}</td>
                <td><a href="${escapeHtml(c.profile_url || '#')}" target="_blank">${escapeHtml(c.nickname || '')}</a></td>
                <td>${intentBadge}</td>
                <td title="${escapeHtml(commentContent)}">${escapeHtml(commentPreview || '-')}</td>
                <td title="${escapeHtml(ipLocation || '')}">${escapeHtml(ipLocation || '-')}</td>
                <td title="${escapeHtml(videoDisplay.title || videoDisplay.subtitle || videoDisplay.sourceVideoUrl || '')}">
                    <div class="small fw-semibold">
                        ${videoDisplay.canOpen
                            ? `<button type="button" class="btn btn-link btn-sm p-0 align-baseline text-start ${videoDisplay.needsWarning ? 'text-warning' : ''}" data-video-url="${escapeHtml(videoDisplay.sourceVideoUrl)}" data-video-warning="${videoDisplay.needsWarning ? '1' : '0'}" data-video-warning-text="${escapeHtml(videoDisplay.warningText || '')}" onclick="openCustomerVideoSource(this)">${escapeHtml(videoPreview || '来源视频')}</button>`
                            : escapeHtml(videoPreview || '暂无视频')}
                    </div>
                    <div class="text-muted small">${escapeHtml(videoDisplay.needsWarning ? (videoDisplay.warningText || videoDisplay.subtitle || '-') : (videoDisplay.subtitle || '-'))}</div>
                </td>
                <td>${renderCustomerStatusInfo(c)}</td>
                <td>${escapeHtml(formatCustomerCommentTime(c.comment_time))}</td>
                <td>${c.created_at ? new Date(c.created_at).toLocaleString() : '-'}</td>
                <td>
                    <button
                        class="btn btn-sm btn-outline-primary"
                        data-sec-uid="${escapeHtml(c.sec_uid || '')}"
                        data-platform="${escapeHtml(c.platform || 'douyin')}"
                        data-customer-name="${escapeHtml(c.nickname || c.sec_uid || '')}"
                        onclick="viewCustomerDetail(this.dataset.secUid, this.dataset.platform, this.dataset.customerName)"
                    >查看</button>
                </td>
            `;
            const checkbox = tr.querySelector('.customer-select-checkbox');
            if (checkbox && selectable) {
                checkbox.addEventListener('change', (event) => toggleCustomerSelection(c, event.target.checked));
            }
            tbody.appendChild(tr);
        });

        if (customers.length === 0) {
            const tr = document.createElement('tr');
            const emptyMessage = currentTaskOnly
                ? (latestSearchSummary?.task_id ? '本次新增暂无客户数据' : '暂无最近一次爬取任务，无法显示本次新增数据')
                : '暂无客户数据';
            tr.innerHTML = `<td colspan="10" class="text-center text-muted py-4">${emptyMessage}</td>`;
            tbody.appendChild(tr);
        }
        
        updateCustomerStats(data.summary || null, customers);
        updateCustomerPagination();
        syncCustomerSelectionUI();
    } catch (e) {
        console.error("Load customers failed", e);
        const tbody = document.getElementById('customer-list');
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center text-danger py-4">客户数据加载失败<br><small>请检查授权状态或后端服务是否正常运行</small></td></tr>';
        }
        if (!silent) {
            showToast(getRequestErrorMessage(e, '加载客户列表失败'), 'error');
        }
    } finally {
        customerAutoRefreshState.loading = false;
    }
}

async function maybeAutoRefreshCurrentTaskCustomers(data) {
    if (pageIsUnloading || document.visibilityState === 'hidden') return;
    if (customerAutoRefreshState.loading || !isPanelActive('#customer-panel')) return;

    const currentTaskOnly = Boolean(document.getElementById('filter-current-task-only')?.checked);
    if (!currentTaskOnly) {
        customerAutoRefreshState.lastRefreshKey = '';
        return;
    }

    const taskId = String(latestSearchSummary?.task_id || '').trim();
    if (!taskId) return;

    const taskKind = deriveCrawlerTaskKind(data);
    const summaryStatus = String(latestSearchSummary?.status || '');
    const progressCurrent = Number(data?.progress?.current || 0);
    const refreshKey = `${taskId}|${taskKind}|${summaryStatus}|${progressCurrent}`;
    const now = Date.now();
    const shouldRefresh = taskKind === 'search' || refreshKey !== customerAutoRefreshState.lastRefreshKey;

    if (!shouldRefresh) return;
    if (refreshKey === customerAutoRefreshState.lastRefreshKey && now - customerAutoRefreshState.lastRefreshAt < 2500) {
        return;
    }

    customerAutoRefreshState.lastRefreshKey = refreshKey;
    customerAutoRefreshState.lastRefreshAt = now;
    await loadCustomers(customerPageState.page, { silent: true });
}

function buildCustomerFilterParams({ includePagination = false } = {}) {
    const query = document.getElementById('customer-search')?.value?.trim() || '';
    const customerTimeRange = buildCustomerCommentTimeRange();
    const currentTaskOnly = Boolean(document.getElementById('filter-current-task-only')?.checked);
    const platform = document.getElementById('filter-platform')?.value || '';
    const status = document.getElementById('filter-status')?.value || '';
    const interactStatus = document.getElementById('filter-interact-status')?.value || '';
    const pageSize = parseInt(document.getElementById('customer-page-size')?.value || '20', 10);
    const params = new URLSearchParams();

    if (includePagination) {
        params.set('page', String(Math.max(customerPageState.page || 1, 1)));
        params.set('page_size', String(Math.max(pageSize, 1)));
    }
    if (platform) params.set('platform', platform);
    if (status) params.set('status', status);
    if (interactStatus) params.set('interact_status', interactStatus);
    if (query) params.set('q', query);
    if (customerTimeRange.comment_time_start) params.set('comment_time_start', customerTimeRange.comment_time_start);
    if (customerTimeRange.comment_time_end) params.set('comment_time_end', customerTimeRange.comment_time_end);
    if (currentTaskOnly) {
        params.set('created_by_task_id', latestSearchSummary?.task_id || '__no_current_task__');
    }
    return params;
}

async function triggerExcelDownload(url, fallbackFilename, options = {}) {
    const res = await fetchWithTimeout(url, options, 60000);
    if (!res.ok) {
        let message = '导出失败';
        try {
            const payload = await res.clone().json();
            message = payload?.detail || payload?.message || message;
        } catch (e) {
            message = await res.text() || message;
        }
        throw new Error(message);
    }

    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const basicMatch = disposition.match(/filename=([^;]+)/i);
    const fileName = utf8Match?.[1]
        ? decodeURIComponent(utf8Match[1])
        : (basicMatch?.[1] || fallbackFilename).replace(/(^"|"$)/g, '');
    const downloadUrl = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = fileName || fallbackFilename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(downloadUrl);
}

async function exportCustomersExcel() {
    try {
        const params = buildCustomerFilterParams();
        await triggerExcelDownload(`${API_BASE}/customers/export?${params.toString()}`, '客户列表.xlsx');
        showToast('客户列表 Excel 导出成功', 'success');
    } catch (e) {
        showToast(`客户列表导出失败: ${e.message}`, 'error');
    }
}

async function exportSelectedCustomersExcel() {
    try {
        const items = Array.from(selectedCustomerMap.values()).map(item => ({
            sec_uid: item.sec_uid,
            platform: item.platform || 'douyin',
        }));
        if (!items.length) {
            showToast('请先勾选要导出的客户', 'warning');
            return;
        }
        await triggerExcelDownload(
            `${API_BASE}/customers/export-selected`,
            '选中客户.xlsx',
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items }),
            }
        );
        showToast(`已导出 ${items.length} 位选中客户`, 'success');
    } catch (e) {
        showToast(`选中客户导出失败: ${e.message}`, 'error');
    }
}

function updateCustomerStats(summary, customers = []) {
    const total = summary?.total ?? customers.length;
    const sent = summary?.sent ?? customers.filter(c => c.status === 'sent').length;
    const replied = summary?.replied ?? customers.filter(c => c.status === 'replied').length;
    const intentDistribution = summary?.intent_distribution || {};
    const intentA = intentDistribution.A ?? customers.filter(c => c.intent_level === 'A').length;
    
    const setElementText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };
    
    setElementText('stat-total', total);
    setElementText('stat-sent', sent);
    setElementText('stat-replied', replied);
    setElementText('stat-intent-a', intentA);
    
    // 意向分布
    const levels = ['A', 'B', 'C', 'D', 'E'];
    levels.forEach(level => {
        const count = intentDistribution[level] ?? customers.filter(c => c.intent_level === level).length;
        const el = document.getElementById(`count-${level}`);
        if (el) el.textContent = count;
    });
}

function updateCustomerPagination() {
    const summaryEl = document.getElementById('customer-page-summary');
    const indicatorEl = document.getElementById('customer-page-indicator');
    const prevBtn = document.getElementById('customer-prev-page');
    const nextBtn = document.getElementById('customer-next-page');
    const start = customerPageState.total === 0 ? 0 : ((customerPageState.page - 1) * customerPageState.pageSize + 1);
    const end = Math.min(customerPageState.page * customerPageState.pageSize, customerPageState.total);

    if (summaryEl) {
        const prefix = customerPageState.query ? `搜索“${customerPageState.query}”` : '显示';
        summaryEl.textContent = `${prefix} ${start}-${end} / 共 ${customerPageState.total} 条`;
    }
    if (indicatorEl) {
        indicatorEl.textContent = `${customerPageState.page} / ${customerPageState.totalPages}`;
    }
    if (prevBtn) {
        prevBtn.disabled = customerPageState.page <= 1;
    }
    if (nextBtn) {
        nextBtn.disabled = customerPageState.page >= customerPageState.totalPages;
    }
}

async function fetchJson(url, options = {}, timeout = 15000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(new Error(`Request timeout after ${timeout}ms`)), timeout);
    try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        const data = await response.json().catch(() => ({}));
        return { ok: response.ok, status: response.status, data };
    } finally {
        clearTimeout(timeoutId);
    }
}

function buildBrowserRuntimeUiState(runtime) {
    const activeBrowser = runtime?.browser_context || runtime?.bot_runtime?.browser_context || null;
    const botRuntime = runtime?.bot_runtime || {};
    const crawlerRuntime = runtime?.crawler_runtime || {};
    const activeBrowserId = runtime?.browser_context_id || activeBrowser?.id || '';
    const activeBrowserName = activeBrowser?.display_name || activeBrowserId || '当前浏览器';
    const crawlerReady = Boolean(crawlerRuntime.browser_ready);
    const crawlerStalled = Boolean(crawlerRuntime.stalled);
    const browserRunning = Boolean(botRuntime.browser_running);
    const effectiveLoggedIn = Boolean(botRuntime.is_logged_in || crawlerReady);

    let tone = 'secondary';
    let summary = '请先打开网页并使用扫码或 Cookies 完成登录。';

    if (!browserRunning && !crawlerReady) {
        tone = 'warning';
        summary = '当前浏览器尚未打开，请先打开网页后使用扫码或 Cookies 登录。';
    } else if (crawlerStalled) {
        tone = 'danger';
        summary = crawlerRuntime.stall_reason || '独立爬取进程疑似卡住，系统将尝试自动恢复。';
    } else if (!effectiveLoggedIn) {
        tone = 'warning';
        summary = `${activeBrowserName}尚未检测到可用网页登录态，请先扫码登录或导入 Cookies。`;
    } else if (crawlerReady) {
        tone = 'success';
        summary = `${activeBrowserName}已就绪，可以开始运行。`;
    } else {
        tone = 'info';
        summary = `${activeBrowserName}已打开，请完成扫码登录或确认 Cookies 有效。`;
    }

    const details = [];
    if (botRuntime.browser_user_data_dir) {
        details.push(`当前 profile：${botRuntime.browser_user_data_dir}`);
    }
    if (botRuntime.is_logged_in) {
        details.push('网页登录态：主浏览器可用');
    } else if (crawlerReady) {
        details.push('网页登录态：crawler 已接管');
    }

    return {
        tone,
        summary,
        details,
        activeBrowser,
        botRuntime,
        crawlerRuntime,
        activeBrowserId,
        activeBrowserName,
        crawlerReady,
        crawlerStalled,
        effectiveLoggedIn,
        browserRunning,
    };
}

function renderBrowserRuntime(runtime) {
    const state = buildBrowserRuntimeUiState(runtime);
    return state;
}

function buildBrowserRuntimePayload(data = {}) {
    const crawlerProcess = data.crawler_process || {};
    return {
        browser_context: data.browser_context || null,
        browser_context_id: data.browser_context_id || data.browser_context?.id || '',
        bot_runtime: {
            browser_context: data.browser_context || null,
            browser_user_data_dir: data.browser_user_data_dir || '',
            browser_running: data.is_running,
            is_logged_in: data.is_logged_in,
        },
        crawler_runtime: {
            stalled: Boolean(crawlerProcess.stalled),
            stall_reason: crawlerProcess.stall_reason || '',
            stall_duration_seconds: Number(crawlerProcess.stall_duration_seconds || 0),
            force_reset_recommended: Boolean(crawlerProcess.force_reset_recommended),
            login_transfer_matched: Object.prototype.hasOwnProperty.call(crawlerProcess, 'login_transfer_matched')
                ? Boolean(crawlerProcess.login_transfer_matched)
                : false,
            browser_ready: Boolean(crawlerProcess.browser_ready),
        },
    };
}

async function ensureBrowserReadyForAction(actionLabel = '开始运行') {
    const result = await fetchJson(`${API_BASE}/status`, {}, 45000);
    if (!result.ok) {
        throw new Error(result.data?.message || '获取运行状态失败');
    }

    const runtime = buildBrowserRuntimePayload(result.data || {});
    renderBrowserRuntime(runtime);

    const state = buildBrowserRuntimeUiState(runtime);

    if (!state.effectiveLoggedIn) {
        throw new Error(`${state.activeBrowserName}尚未检测到可用网页登录态，请先扫码登录或导入 Cookies 后再${actionLabel}`);
    }

    return runtime;
}

async function ensureBrowserContextReadyForCrawlerStart(actionLabel = '开始运行') {
    const result = await fetchJson(`${API_BASE}/status`, {}, 45000);
    if (!result.ok) {
        throw new Error(result.data?.message || '获取运行状态失败');
    }

    const runtime = buildBrowserRuntimePayload(result.data || {});
    renderBrowserRuntime(runtime);

    const state = buildBrowserRuntimeUiState(runtime);

    if (!state.browserRunning && !state.crawlerReady) {
        throw new Error(`${state.activeBrowserName}尚未准备好，请先打开网页并完成登录后再${actionLabel}`);
    }

    return runtime;
}

function getStatusBadge(status) {
    switch(status) {
        case 'sent': return 'bg-success';
        case 'replied': return 'bg-primary';
        case 'pending': return 'bg-warning text-dark';
        case 'failed': return 'bg-danger';
        default: return 'bg-secondary';
    }
}

function getInteractStatusBadge(status) {
    switch(status) {
        case 'interacted': return 'bg-info text-dark';
        case 'failed': return 'bg-danger';
        case 'pending': return 'bg-secondary';
        default: return 'bg-secondary';
    }
}

// ========== 消息同步功能 ==========


function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function sanitizePercent(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return 0;
    return Math.max(0, Math.min(100, Math.round(num)));
}

function formatLeadScoreLabel(leadScore) {
    const labels = {
        hot: '热线索',
        warm: '温线索',
        cool: '冷启线索',
        cold: '低优先级',
    };
    return labels[leadScore] || leadScore || '未知';
}

// 加载增强分析数据
async function loadEnhancedAnalytics() {
    try {
        const highIntentList = document.getElementById('high-intent-list');
        const actionItemsList = document.getElementById('action-items-list');

        if (highIntentList) {
            highIntentList.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-4">正在加载数据...</td></tr>';
        }
        if (actionItemsList) {
            actionItemsList.innerHTML = '<div class="text-center text-muted py-4">正在加载...</div>';
        }

        const dashboardRes = await fetchWithTimeout(`${API_BASE}/enhanced-analytics/dashboard`, {}, 30000);
        const dashboardData = await dashboardRes.json();

        if (!dashboardData?.success || !dashboardData?.data) {
            throw new Error(dashboardData?.message || '获取数据失败');
        }

        const data = dashboardData.data;
        const actionItems = data.actions?.items || [];
        const summaryCustomers = data.overview?.high_intent_customers || [];

        updateActionItems(actionItems);

        if (summaryCustomers.length > 0) {
            updateHighIntentCustomersList(summaryCustomers);
        } else {
            await loadHighIntentCustomersSilently();
        }
    } catch (e) {
        console.error('Load enhanced analytics failed', e);
        const highIntentList = document.getElementById('high-intent-list');
        const actionItemsList = document.getElementById('action-items-list');

        if (highIntentList) {
            highIntentList.innerHTML =
                '<tr><td colspan="4" class="text-center text-danger py-4">数据加载失败<br><small>请检查后端服务是否正常运行</small></td></tr>';
        }
        if (actionItemsList) {
            actionItemsList.innerHTML = '<div class="text-center text-danger py-4">加载失败</div>';
        }
    }
}

// 新的侧边栏导航切换逻辑
function switchPanel(panelId) {
    if (panelId === 'learning-panel') {
        panelId = 'knowledge-panel';
    }

    // 隐藏所有面板
    document.querySelectorAll('.tab-pane').forEach(panel => {
        panel.classList.remove('show', 'active');
    });
    
    // 显示目标面板
    const targetPanel = document.getElementById(panelId);
    if (targetPanel && !targetPanel.classList.contains('hidden')) {
        targetPanel.classList.add('show', 'active');
    } else {
        const fallbackPanel = document.getElementById('crawl-panel');
        if (fallbackPanel) {
            panelId = 'crawl-panel';
            fallbackPanel.classList.add('show', 'active');
        }
    }
    
    // 更新侧边栏菜单高亮
    document.querySelectorAll('.sider-menu-link').forEach(link => {
        if (link.getAttribute('data-target') === panelId) {
            link.classList.add('active');
            
            // 更新面包屑
            const panelName = link.querySelector('span').innerText;
            const breadcrumb = document.getElementById('header-breadcrumb');
            if (breadcrumb) {
                breadcrumb.innerHTML = `
                    <li class="breadcrumb-item"><a href="#">首页</a></li>
                    <li class="breadcrumb-item active" aria-current="page">${panelName}</li>
                `;
            }
        } else {
            link.classList.remove('active');
        }
    });

    // 触发原来的标签页切换事件，兼容现有代码
    if (panelId === 'crawl-panel') {
        // 如果全局变量 isMonitoring 已知，则立即更新 UI，避免等待轮询
        if (typeof isMonitoring !== 'undefined') {
            updateMonitorStatus(isMonitoring, 0);
        }
        // 立即触发一次状态轮询，确保状态最新
        if (typeof pollStatus === 'function') {
            pollStatus();
        }
    }

    if (panelId === 'analytics-panel') {
        loadEnhancedAnalytics();
    }
    
    if (panelId === 'customer-panel') {
        loadCustomers();
    }

    // 触发知识库/学习中心的加载逻辑
    if (panelId === 'knowledge-panel' && typeof KnowledgeApp !== 'undefined') {
        KnowledgeApp.onPanelActivated();
    }
    
    if (panelId === 'learning-panel') {
        if (typeof onLearningPanelActivated === 'function') {
            onLearningPanelActivated();
        }
    }
}

async function loadHighIntentCustomersSilently(limit = 10) {
    const tbody = document.getElementById('high-intent-list');
    if (tbody && !tbody.children.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-4">正在加载高意向客户...</td></tr>';
    }

    const res = await fetchWithTimeout(`${API_BASE}/analytics/high-intent-customers?min_score=50&limit=${limit}`, {}, 30000);
    const data = await res.json();
    if (data && Array.isArray(data.customers)) {
        updateHighIntentCustomersList(data.customers);
    }
}

// 页面加载完成后初始化基础状态
document.addEventListener('DOMContentLoaded', function() {
    updateMonitorStatus();
});

// 标签页切换时刷新数据
document.addEventListener('shown.bs.tab', function(event) {
    const targetId = event.target.getAttribute('data-bs-target');
    
    if (targetId === '#analytics-panel') {
        loadEnhancedAnalytics();
    }

    if (targetId === '#customer-panel') {
        loadCustomers();
    }
    
    if (targetId === '#knowledge-panel') {
        if (typeof onKnowledgePanelActivated === 'function') {
            Promise.resolve(onKnowledgePanelActivated()).catch(err => {
                console.error('Activate knowledge panel failed', err);
            });
        }
    }

    if (targetId === '#learning-panel') {
        if (typeof onLearningPanelActivated === 'function') {
            Promise.resolve(onLearningPanelActivated()).catch(err => {
                console.error('Activate learning panel failed', err);
            });
        }
    }
});

function isPanelActive(panelSelector) {
    const panel = document.querySelector(panelSelector);
    return Boolean(panel && panel.classList.contains('active') && panel.classList.contains('show'));
}

// 更新高意向客户列表
function updateHighIntentCustomersList(customers) {
    const tbody = document.getElementById('high-intent-list');
    if (!tbody) return;
    
    if (!customers || customers.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-4">暂无高意向客户数据<br><small>请先同步客户消息并分析意向</small></td></tr>';
        return;
    }
    
    tbody.innerHTML = '';
    customers.forEach((c, index) => {
        const customerName = c.customer_name || c.name || '未知客户';
        const customerId = c.customer_id || c.id || customerName;
        const industry = c.industry || c.industry_profile?.primary_industry || 'unknown';
        const buyingStage = c.buying_stage || 'awareness';
        const intentScore = Number(c.intent_score ?? c.score ?? 0);
        const profileScore = Number(c.overall_profile_score ?? 0);
        const probability = (c.purchase_probability || 0) * 100;
        const decisionStyle = c.decision_style || 'unknown';
        const leadScoreLabel = formatLeadScoreLabel(c.lead_score || 'cold');
        const intentLevel = String(c.intent_level || '').toUpperCase();
        
        const industryLabels = {
            'ecommerce': '电商', 'education': '教育', 'finance': '金融',
            'technology': '科技', 'retail': '零售', 'services': '服务',
            'healthcare': '医疗', 'manufacturing': '制造', 'realestate': '房产',
            'unknown': '未知'
        };
        
        const stageLabels = {
            'awareness': '认知', 'interest': '兴趣', 'evaluation': '评估',
            'decision': '决策', 'purchase': '购买'
        };
        
        const styleLabels = {
            'analytical': '分析型', 'directive': '指令型',
            'conceptual': '概念型', 'behavioral': '行为型', 'unknown': '未知'
        };
        
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>
                <div class="d-flex align-items-center">
                    <div class="avatar-sm me-2" style="width: 32px; height: 32px; border-radius: 50%; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; color: white; font-size: 12px;">
                        ${escapeHtml(customerName.charAt(0))}
                    </div>
                    <div>
                        <a href="#" class="fw-bold text-decoration-none customer-profile-link" data-customer-id="${escapeHtml(customerId)}">
                            ${escapeHtml(customerName)}
                        </a>
                        <div class="small text-muted">${escapeHtml(industryLabels[industry] || industry)}</div>
                    </div>
                </div>
            </td>
            <td>
                <div class="d-flex flex-column">
                    <span class="badge bg-info align-self-start">${escapeHtml(stageLabels[buyingStage] || buyingStage)}</span>
                    <small class="text-muted mt-1">${escapeHtml(styleLabels[decisionStyle] || decisionStyle)}</small>
                </div>
            </td>
            <td>
                <div class="d-flex flex-column">
                    <span class="badge ${probability >= 70 ? 'bg-success' : probability >= 40 ? 'bg-warning' : 'bg-secondary'}">${probability.toFixed(0)}%</span>
                    <small class="text-muted mt-1">意向分 ${intentScore.toFixed(0)} · ${escapeHtml(leadScoreLabel)}${intentLevel ? ` · ${escapeHtml(intentLevel)}级` : ''}</small>
                    <small class="text-muted">客户评分 ${profileScore.toFixed(0)}</small>
                </div>
            </td>
            <td>
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-primary btn-customer-profile" data-customer-id="${escapeHtml(customerId)}" title="查看客户画像">客户画像</button>
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
    
    tbody.querySelectorAll('.customer-profile-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            showEnhancedProfile(link.dataset.customerId);
        });
    });
    tbody.querySelectorAll('.btn-customer-profile').forEach(btn => {
        btn.addEventListener('click', () => showEnhancedProfile(btn.dataset.customerId));
    });
}

/**
 * 刷新高意向客户列表
 */
async function refreshHighIntentCustomers() {
    try {
        const minScore = document.getElementById('filter-min-score')?.value || 50;
        const url = `${API_BASE}/analytics/high-intent-customers?min_score=${minScore}&limit=20`;

        const res = await fetchWithTimeout(url);
        const data = await res.json();
        
        if (data.customers) {
            updateHighIntentCustomersList(data.customers);
            showToast(`已刷新 ${data.total} 位客户`, 'success');
        }
    } catch (e) {
        showToast('刷新失败: ' + e.message, 'error');
    }
}

async function exportHighIntentCustomersExcel() {
    try {
        const minScore = document.getElementById('filter-min-score')?.value || 50;
        await triggerExcelDownload(
            `${API_BASE}/analytics/high-intent-customers/export?min_score=${encodeURIComponent(minScore)}&limit=500`,
            '高意向客户.xlsx'
        );
        showToast('高意向客户 Excel 导出成功', 'success');
    } catch (e) {
        showToast(`高意向客户导出失败: ${e.message}`, 'error');
    }
}

async function exportFollowUpCustomersExcel() {
    try {
        await triggerExcelDownload(
            `${API_BASE}/enhanced-analytics/follow-up/export`,
            '待跟进客户.xlsx'
        );
        showToast('待跟进客户 Excel 导出成功', 'success');
    } catch (e) {
        showToast(`待跟进客户导出失败: ${e.message}`, 'error');
    }
}

function formatAnalyticsTime(value) {
    if (!value) return '暂无';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return String(value);
    }
    return date.toLocaleString();
}

function getTimelineUrgencyLabel(urgency) {
    const labels = { immediate: '紧急', short: '短期', medium: '中期', long: '长期', unknown: '未知' };
    return labels[urgency] || escapeHtml(String(urgency || '未知'));
}

function getDirectionLabel(direction) {
    const labels = { inbound: '客户消息', outbound: '系统回复', unknown: '未知' };
    return labels[direction] || escapeHtml(String(direction || '未知'));
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

async function openFollowUpInDouyinBrowser(customerName = '') {
    const targetUrl = 'https://www.douyin.com/chat';
    let browserRunning = false;

    try {
        const statusResult = await fetchJson(`${API_BASE}/status`, {}, 5000);
        if (statusResult.ok) {
            browserRunning = Boolean(
                statusResult.data?.is_running
                || statusResult.data?.browser_state === 'running'
            );
        }
    } catch (e) {
        // 状态检查失败时直接走打开链路，由后端负责自动启动浏览器。
    }

    showToast(
        browserRunning
            ? '检测到抖音浏览器已启动，正在切换到前台并打开聊天页...'
            : '未检测到抖音浏览器，正在启动并打开聊天页...',
        'info'
    );

    const openResult = await fetchJson(`${API_BASE}/browser/open_url`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: targetUrl }),
    }, 70000);

    if (!openResult.ok || !openResult.data?.success) {
        throw new Error(openResult.data?.message || '无法打开抖音聊天页面');
    }

    showToast(
        browserRunning
            ? `已切换到抖音浏览器前台，可继续跟进 ${customerName || '当前客户'}`
            : `已启动抖音浏览器并打开聊天页面，可继续跟进 ${customerName || '当前客户'}`,
        'success'
    );
}

function getStatusLabel(status) {
    const labels = {
        pending: '待发送私信',
        sent: '已发送私信',
        replied: '客户已回复',
        failed: '失败',
    };
    return labels[status] || escapeHtml(String(status || '未设置'));
}

function getInteractStatusLabel(status) {
    const labels = {
        pending: '未互动',
        interacted: '已互动',
        failed: '互动失败',
    };
    return labels[status] || escapeHtml(String(status || '未设置'));
}

function renderCustomerStatusInfo(customer = {}) {
    const directStatus = customer.status || 'pending';
    const interactStatus = customer.interact_status || 'pending';
    return `
        <div class="d-flex flex-column gap-1">
            <div><span class="badge ${getStatusBadge(directStatus)}">私信 ${escapeHtml(getStatusLabel(directStatus))}</span></div>
            <div><span class="badge ${getInteractStatusBadge(interactStatus)}">互动 ${escapeHtml(getInteractStatusLabel(interactStatus))}</span></div>
        </div>
    `;
}

function getInteractionHeatBadgeClass(label) {
    switch (String(label || '').toLowerCase()) {
        case '高': return 'bg-danger';
        case '中': return 'bg-warning text-dark';
        case '低': return 'bg-secondary';
        default: return 'bg-light text-dark';
    }
}

function renderTagBadges(tags, badgeClass = 'bg-secondary', emptyText = '暂无标签') {
    const normalized = Array.isArray(tags)
        ? tags.map(tag => String(tag || '').trim()).filter(Boolean)
        : [];
    return normalized.length
        ? normalized.map(tag => `<span class="badge ${badgeClass} me-1 mb-1">${escapeHtml(tag)}</span>`).join('')
        : `<span class="text-muted">${escapeHtml(emptyText)}</span>`;
}

function renderEnhancedProfileModal(profile = {}, customerId = '') {
    const customer = profile.customer_summary || {};
    const messageSummary = profile.message_summary || {};
    const displayMetrics = profile.display_metrics || {};
    const interactionHeat = displayMetrics.interaction_heat || {};
    const recentMessages = Array.isArray(messageSummary.recent_messages) ? messageSummary.recent_messages : [];
    const tags = Array.isArray(customer.tags) ? customer.tags : [];
    const primaryInterests = Array.isArray(profile.interest_topics) ? profile.interest_topics.slice(0, 4) : [];
    const topPainPoints = Array.isArray(profile.pain_points) ? profile.pain_points.slice(0, 4) : [];
    const budgetSignals = Array.isArray(profile.budget_indicators?.signals) ? profile.budget_indicators.signals : [];
    const timelineSignals = Array.isArray(profile.timeline_indicators?.signals) ? profile.timeline_indicators.signals : [];
    const recommendationApproach = Array.isArray(profile.recommendation?.approach) ? profile.recommendation.approach : [];
    const recommendationPoints = Array.isArray(profile.recommendation?.key_points) ? profile.recommendation.key_points : [];
    const riskFactors = Array.isArray(profile.recommendation?.risk_factors) ? profile.recommendation.risk_factors : [];
    const profileName = profile.customer_name || customer.nickname || customerId || '未知客户';
    const quickSummary = recommendationApproach[0]
        || recommendationPoints[0]
        || messageSummary.latest_message_preview
        || customer.comment_content
        || '当前画像已生成，下面展示的是客户当前的重点信息与分析结果。';
    const status = displayMetrics.status || customer.status || 'pending';
    const interactStatus = customer.interact_status || 'pending';
    const intentLevel = String(displayMetrics.intent_level || customer.intent_level || '').toUpperCase();
    const intentScore = Number(displayMetrics.intent_score ?? customer.intent_score);
    const rawIntentScore = Number(displayMetrics.raw_intent_score ?? customer.raw_intent_score ?? customer.intent_score);
    const profileScore = Number(displayMetrics.overall_profile_score ?? profile.overall_score);
    const intentScoreDisplay = Number.isFinite(intentScore) ? intentScore.toFixed(0) : '暂无';
    const rawIntentScoreDisplay = Number.isFinite(rawIntentScore) ? rawIntentScore.toFixed(0) : '暂无';
    const profileScoreDisplay = Number.isFinite(profileScore) ? profileScore.toFixed(0) : '暂无';
    const sourceVideoUrl = getPreferredCustomerVideoUrl(customer);
    const totalMessages = Number(interactionHeat.total_messages || messageSummary.total_messages || 0);
    const inboundCount = Number(interactionHeat.inbound_count || messageSummary.inbound_count || 0);
    const heatLabel = interactionHeat.label || (totalMessages > 0 ? '低' : '暂无');
    const heatScore = Number.isFinite(Number(interactionHeat.score)) ? Number(interactionHeat.score).toFixed(0) : '暂无';
    const customerDisplayId = customer.sec_uid || customer.customer_id || profile.customer_id || customerId || '';
    const platform = customer.platform || 'douyin';
    const scoreTone = Number(intentScore || 0) >= 70
        ? 'success'
        : Number(intentScore || 0) >= 50
            ? 'warning text-dark'
            : 'secondary';

    return `
        <div class="modal fade" id="enhancedProfileModal" tabindex="-1">
            <div class="modal-dialog modal-xl modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <div>
                            <h5 class="modal-title">客户画像 - ${escapeHtml(profileName)}</h5>
                            <div class="small text-muted mt-1">
                                ${escapeHtml(platform)} / ${escapeHtml(customer.unique_id || customer.sec_uid || profile.customer_id || customerId)}
                                ${profile.analysis_generated_at ? ` · 生成时间 ${escapeHtml(formatAnalyticsTime(profile.analysis_generated_at))}` : ''}
                            </div>
                        </div>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <div class="alert alert-light border mb-3">
                            <div class="d-flex flex-column flex-lg-row justify-content-between gap-3">
                                <div class="flex-grow-1">
                                    <div class="small text-muted mb-1">重点摘要</div>
                                    <div class="fw-semibold">${escapeHtml(quickSummary)}</div>
                                    <div class="small text-muted mt-2">
                                        建议 ${escapeHtml(getTimingLabel(profile.recommendation?.timing))}
                                        · ${escapeHtml(getStageLabel(profile.buying_stage?.current_stage))}
                                        · 预计 ${escapeHtml(profile.timeline_indicators?.estimated_timeline || '时间待确认')}
                                    </div>
                                </div>
                                <div class="text-lg-end">
                                    <span class="badge ${getStatusBadge(status)} me-2">${escapeHtml(getStatusLabel(status))}</span>
                                    <span class="badge ${getInteractStatusBadge(interactStatus)} me-2">${escapeHtml(getInteractStatusLabel(interactStatus))}</span>
                                    <span class="badge ${getIntentLevelBadgeClass(intentLevel)}">${intentLevel ? `${escapeHtml(intentLevel)}级` : '未评级'}</span>
                                    <div class="small text-muted mt-2">
                                        最新互动 ${escapeHtml(formatAnalyticsTime(messageSummary.latest_message_at || customer.comment_time || customer.updated_at))}
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="row g-3 mb-3">
                            <div class="col-sm-6 col-xl-3">
                                <div class="card h-100 border-0 bg-light">
                                    <div class="card-body">
                                        <div class="small text-muted">客户状态</div>
                                        <div class="mt-2 fw-semibold">私信：${escapeHtml(getStatusLabel(status))}</div>
                                        <div class="small text-muted mt-1">互动：${escapeHtml(getInteractStatusLabel(interactStatus))}</div>
                                        <div class="small text-muted mt-2">优先级 ${escapeHtml(getPriorityLabel(profile.recommendation?.priority))}</div>
                                    </div>
                                </div>
                            </div>
                            <div class="col-sm-6 col-xl-3">
                                <div class="card h-100 border-0 bg-light">
                                    <div class="card-body">
                                        <div class="small text-muted">互动热度</div>
                                        <div class="mt-2"><span class="badge ${getInteractionHeatBadgeClass(heatLabel)} fs-6">${escapeHtml(heatLabel)}</span></div>
                                        <div class="small text-muted mt-2">热度分 ${escapeHtml(heatScore)} · 互动 ${escapeHtml(String(totalMessages))} 条</div>
                                    </div>
                                </div>
                            </div>
                            <div class="col-sm-6 col-xl-3">
                                <div class="card h-100 border-0 bg-light">
                                    <div class="card-body">
                                        <div class="small text-muted">意向等级</div>
                                        <div class="mt-2"><span class="badge ${getIntentLevelBadgeClass(intentLevel)} fs-6">${intentLevel ? `${escapeHtml(intentLevel)}级` : '未评级'}</span></div>
                                        <div class="small text-muted mt-2">客户发言 ${escapeHtml(String(inboundCount))} 条</div>
                                    </div>
                                </div>
                            </div>
                            <div class="col-sm-6 col-xl-3">
                                <div class="card h-100 border-0 bg-light">
                                    <div class="card-body">
                                        <div class="small text-muted">意向分数</div>
                                        <div class="mt-2"><span class="badge bg-${scoreTone} fs-6">${escapeHtml(intentScoreDisplay)}</span></div>
                                        <div class="small text-muted mt-2">识别分 ${escapeHtml(rawIntentScoreDisplay)} · 客户评分 ${escapeHtml(profileScoreDisplay)}</div>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="row g-3 mb-3">
                            <div class="col-lg-5">
                                <div class="card h-100">
                                    <div class="card-header py-2">重点信息</div>
                                    <div class="card-body py-2">
                                        <table class="table table-sm align-middle mb-3">
                                            <tr><td>客户昵称</td><td>${escapeHtml(customer.nickname || profile.customer_name || '未知客户')}</td></tr>
                                            <tr><td>客户ID</td><td class="text-break">${escapeHtml(customerDisplayId || '-')}</td></tr>
                                            <tr><td>唯一号</td><td>${escapeHtml(customer.unique_id || '-')}</td></tr>
                                            <tr><td>企业/行业</td><td>${escapeHtml(customer.company || profile.company_profile?.company_name || '-')}${profile.industry_profile?.primary_industry ? ` / ${escapeHtml(getIndustryLabel(profile.industry_profile?.primary_industry))}` : ''}</td></tr>
                                            <tr><td>评论时间</td><td>${escapeHtml(formatAnalyticsTime(customer.comment_time))}</td></tr>
                                            <tr><td>最近客户消息</td><td>${escapeHtml(formatAnalyticsTime(messageSummary.latest_inbound_at))}</td></tr>
                                            <tr><td>更新时间</td><td>${escapeHtml(formatAnalyticsTime(customer.updated_at || profile.analysis_generated_at))}</td></tr>
                                        </table>
                                        <div class="mb-2">
                                            <div class="small text-muted mb-1">当前标签</div>
                                            <div>${renderTagBadges(tags, 'bg-secondary', '暂无标签')}</div>
                                        </div>
                                        ${customer.comment_content ? `
                                            <div class="mb-2">
                                                <div class="small text-muted mb-1">评论内容</div>
                                                <div class="border rounded bg-light p-2 small">${escapeHtml(customer.comment_content)}</div>
                                            </div>
                                        ` : ''}
                                        ${messageSummary.latest_message_preview ? `
                                            <div class="mb-0">
                                                <div class="small text-muted mb-1">最近消息摘要</div>
                                                <div class="border rounded bg-light p-2 small">${escapeHtml(messageSummary.latest_message_preview)}</div>
                                            </div>
                                        ` : ''}
                                    </div>
                                </div>
                            </div>
                            <div class="col-lg-7">
                                <div class="card h-100">
                                    <div class="card-header py-2">画像概览</div>
                                    <div class="card-body py-2">
                                        <div class="row g-3">
                                            <div class="col-md-6">
                                                <div class="border rounded p-3 h-100 bg-light">
                                                    <div class="small text-muted mb-1">核心判断</div>
                                                    <div class="mb-2">
                                                        <span class="badge ${getIntentLevelBadgeClass(intentLevel)} me-2">${intentLevel ? `${escapeHtml(intentLevel)}级意向` : '未评级'}</span>
                                                        <span class="badge ${getInteractionHeatBadgeClass(heatLabel)}">${escapeHtml(heatLabel)}互动</span>
                                                    </div>
                                                    <div class="small text-muted">当前客户更适合 ${escapeHtml(getTimingLabel(profile.recommendation?.timing))}，建议围绕 ${escapeHtml(getStageLabel(profile.buying_stage?.current_stage))} 阶段做沟通。</div>
                                                </div>
                                            </div>
                                            <div class="col-md-6">
                                                <div class="border rounded p-3 h-100 bg-light">
                                                    <div class="small text-muted mb-1">沟通建议</div>
                                                    <div class="small">${escapeHtml(recommendationPoints[0] || recommendationApproach[0] || '建议继续围绕客户最近关注点做轻量沟通。')}</div>
                                                </div>
                                            </div>
                                            <div class="col-12">
                                                <div class="d-flex flex-wrap gap-2">
                                                    ${customer.profile_url ? `<a class="btn btn-outline-primary" href="${escapeHtml(customer.profile_url)}" target="_blank" rel="noopener noreferrer">打开客户主页</a>` : ''}
                                                    ${sourceVideoUrl ? `<a class="btn btn-outline-secondary" href="${escapeHtml(sourceVideoUrl)}" target="_blank" rel="noopener noreferrer">打开来源视频</a>` : ''}
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="card mb-3">
                            <div class="card-header py-2">系统建议</div>
                            <div class="card-body py-2">
                                <div class="row g-3">
                                    <div class="col-lg-4">
                                        <div class="small text-muted mb-1">建议动作</div>
                                        ${recommendationApproach.length
                                            ? recommendationApproach.slice(0, 4).map(item => `<div class="small mb-1">- ${escapeHtml(item)}</div>`).join('')
                                            : '<div class="small text-muted">暂无建议动作</div>'}
                                    </div>
                                    <div class="col-lg-4">
                                        <div class="small text-muted mb-1">沟通重点</div>
                                        ${recommendationPoints.length
                                            ? recommendationPoints.slice(0, 4).map(item => `<div class="small mb-1">- ${escapeHtml(item)}</div>`).join('')
                                            : '<div class="small text-muted">暂无沟通重点</div>'}
                                    </div>
                                    <div class="col-lg-4">
                                        <div class="small text-muted mb-1">风险提醒</div>
                                        ${riskFactors.length
                                            ? riskFactors.slice(0, 4).map(item => `<span class="badge bg-danger me-1 mb-1">${escapeHtml(item)}</span>`).join('')
                                            : '<span class="text-success small">暂无明显风险</span>'}
                                    </div>
                                </div>
                            </div>
                        </div>

                        <details class="mb-3">
                            <summary class="fw-semibold">查看更多分析细节</summary>
                            <div class="row g-3 mt-2">
                                <div class="col-lg-6">
                                    <div class="card h-100">
                                        <div class="card-header py-2">兴趣与痛点</div>
                                        <div class="card-body py-2">
                                            <div class="mb-3">
                                                <div class="small text-muted mb-1">兴趣主题</div>
                                                ${primaryInterests.length
                                                    ? primaryInterests.map(item => `<div class="mb-1"><span class="badge bg-light text-dark me-1">${escapeHtml(item.topic || '未命名')}</span><small class="text-muted">${escapeHtml(((item.matched_keywords || []).join(' / ')) || '未提取关键词')}</small></div>`).join('')
                                                    : '<div class="text-muted small">暂无兴趣主题</div>'}
                                            </div>
                                            <div>
                                                <div class="small text-muted mb-1">痛点分析</div>
                                                ${topPainPoints.length
                                                    ? topPainPoints.map(item => `<div class="mb-1"><span class="badge bg-warning text-dark me-1">${escapeHtml(item.pain_point || '未命名')}</span><small class="text-muted">${escapeHtml(((item.matched_keywords || []).join(' / ')) || '未提取关键词')}</small></div>`).join('')
                                                    : '<div class="text-muted small">暂无痛点分析</div>'}
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                <div class="col-lg-6">
                                    <div class="card h-100">
                                        <div class="card-header py-2">预算、时机与最近消息</div>
                                        <div class="card-body py-2">
                                            <p class="mb-1"><strong>预算状态:</strong> ${profile.budget_indicators?.has_budget ? '已识别预算' : '预算未明确'}</p>
                                            <p class="mb-1"><strong>预算范围:</strong> ${getBudgetLabel(profile.budget_indicators?.budget_range)}</p>
                                            <p class="mb-1"><strong>紧迫程度:</strong> ${getTimelineUrgencyLabel(profile.timeline_indicators?.urgency)}</p>
                                            <p class="mb-3"><strong>决策风格:</strong> ${getStyleLabel(profile.decision_style?.primary_style)} / ${getCommLabel(profile.communication_preference?.primary_preference)}</p>
                                            <div class="small text-muted mb-1">预算信号</div>
                                            <div class="mb-3">
                                                ${budgetSignals.length
                                                    ? budgetSignals.map(item => `<span class="badge ${item.type === 'negative' ? 'bg-danger' : 'bg-success'} me-1 mb-1">${escapeHtml(item.keyword)}</span>`).join('')
                                                    : '<span class="text-muted small">暂无预算信号</span>'}
                                            </div>
                                            <div class="small text-muted mb-1">时间线信号</div>
                                            <div class="mb-3">
                                                ${timelineSignals.length
                                                    ? timelineSignals.map(item => `<span class="badge bg-warning text-dark me-1 mb-1">${escapeHtml(item)}</span>`).join('')
                                                    : '<span class="text-muted small">暂无时间线信号</span>'}
                                            </div>
                                            <div class="small text-muted mb-1">最近互动记录</div>
                                            ${recentMessages.length
                                                ? recentMessages.map(item => `
                                                    <div class="border rounded p-2 mb-2 bg-light">
                                                        <div class="d-flex justify-content-between align-items-center gap-2 mb-1">
                                                            <span class="badge ${getDirectionBadgeClass(item.direction)}">${getDirectionLabel(item.direction)}</span>
                                                            <small class="text-muted">${escapeHtml(formatAnalyticsTime(item.created_at))}</small>
                                                        </div>
                                                        <div class="small">${escapeHtml(item.content)}</div>
                                                    </div>
                                                `).join('')
                                                : '<div class="text-muted small">暂无最近消息</div>'}
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </details>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

/**
 * 显示增强型客户画像
 */
async function showEnhancedProfile(customerId) {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/analytics/customer/${customerId}/enhanced-profile`);
        const profile = await res.json();

        if (profile.message && !profile.customer_id) {
            showToast(profile.message, 'error');
            return;
        }

        const oldModal = document.getElementById('enhancedProfileModal');
        if (oldModal) oldModal.remove();

        document.body.insertAdjacentHTML('beforeend', renderEnhancedProfileModal(profile, customerId));
        const modalElement = bindModalAccessibility(
            document.getElementById('enhancedProfileModal'),
            { removeOnHidden: true }
        );
        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    } catch (e) {
        showToast('获取客户画像失败: ' + e.message, 'error');
    }
}

// 辅助函数
function getSizeLabel(size) {
    const labels = { 'enterprise': '大型企业', 'mid_market': '中型企业', 'smb': '中小企业', 'startup': '创业公司', 'unknown': '未知' };
    return labels[size] || escapeHtml(String(size || '未知'));
}

function getIndustryLabel(industry) {
    const labels = { 'ecommerce': '电商', 'education': '教育', 'finance': '金融', 'technology': '科技', 'retail': '零售', 'services': '服务', 'healthcare': '医疗', 'manufacturing': '制造', 'realestate': '房产', 'unknown': '未知' };
    return labels[industry] || escapeHtml(String(industry || '未知'));
}

function getStageLabel(stage) {
    const labels = { 'awareness': '认知阶段', 'interest': '兴趣阶段', 'evaluation': '评估阶段', 'decision': '决策阶段', 'purchase': '购买阶段' };
    return labels[stage] || escapeHtml(String(stage || '未知'));
}

function getStyleLabel(style) {
    const labels = { 'analytical': '分析型', 'directive': '指令型', 'conceptual': '概念型', 'behavioral': '行为型', 'unknown': '未知' };
    return labels[style] || escapeHtml(String(style || '未知'));
}

function getCommLabel(pref) {
    const labels = { 'formal': '正式沟通', 'casual': '轻松沟通', 'technical': '技术导向', 'business': '业务导向', 'unknown': '未知' };
    return labels[pref] || escapeHtml(String(pref || '未知'));
}

function getEngagementLabel(pattern) {
    const labels = { 'active': '积极互动', 'moderate': '适度互动', 'passive': '被动互动', 'none': '无互动' };
    return labels[pattern] || escapeHtml(String(pattern || '未知'));
}

function getBudgetLabel(range) {
    const labels = { 'high': '高预算', 'medium': '中等预算', 'low': '低预算', 'unknown': '未知' };
    return labels[range] || escapeHtml(String(range || '未知'));
}

function getPriorityLabel(priority) {
    const labels = { 'high': '高优先级', 'medium': '中优先级', 'low': '低优先级' };
    return labels[priority] || escapeHtml(String(priority || '未知'));
}

function getTimingLabel(timing) {
    const labels = { 'immediate': '立即跟进', 'within_week': '本周跟进', 'normal': '正常跟进', 'nurture': '培育跟进' };
    return labels[timing] || escapeHtml(String(timing || '未知'));
}

function formatResponseTime(minutes) {
    if (!minutes || minutes === 0) return '暂无数据';
    if (minutes < 60) return `${minutes.toFixed(0)}分钟`;
    if (minutes < 1440) return `${(minutes / 60).toFixed(1)}小时`;
    return `${(minutes / 1440).toFixed(1)}天`;
}

/**
 * 分析客户意向
 */
async function analyzeCustomerIntent(customerName) {
    try {
        showToast(`正在分析 ${customerName} 的购买意向...`, 'info');
        
        const res = await fetchWithTimeout(`${API_BASE}/purchase-intent/analyze/${encodeURIComponent(customerName)}`);
        if (res.ok) {
            const data = await res.json();
            if (data.success) {
                const result = data.result || data;
                
                // 显示分析结果模态框
                const modalHtml = `
                    <div class="modal fade" id="intentAnalysisModal" tabindex="-1">
                        <div class="modal-dialog modal-lg">
                            <div class="modal-content">
                                <div class="modal-header bg-primary text-white">
                                    <h5 class="modal-title">📊 购买意向分析 - ${escapeHtml(customerName)}</h5>
                                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="关闭"></button>
                                </div>
                                <div class="modal-body">
                                    <div class="row">
                                        <div class="col-md-6">
                                            <div class="card">
                                                <div class="card-header">BANT评分</div>
                                                <div class="card-body">
                                                    <table class="table table-sm">
                                                        <tr><td>预算(Budget)</td><td><span class="badge bg-${result.bant?.budget >= 20 ? 'success' : 'warning'}">${result.bant?.budget || 0}/25</span></td></tr>
                                                        <tr><td>权限(Authority)</td><td><span class="badge bg-${result.bant?.authority >= 20 ? 'success' : 'warning'}">${result.bant?.authority || 0}/25</span></td></tr>
                                                        <tr><td>需求(Need)</td><td><span class="badge bg-${result.bant?.need >= 20 ? 'success' : 'warning'}">${result.bant?.need || 0}/25</span></td></tr>
                                                        <tr><td>时间线(Timeline)</td><td><span class="badge bg-${result.bant?.timeline >= 20 ? 'success' : 'warning'}">${result.bant?.timeline || 0}/25</span></td></tr>
                                                    </table>
                                                </div>
                                            </div>
                                        </div>
                                        <div class="col-md-6">
                                            <div class="card">
                                                <div class="card-header">意向评分</div>
                                                <div class="card-body text-center">
                                                    <h2 class="text-${result.total_score >= 70 ? 'success' : result.total_score >= 50 ? 'warning' : 'secondary'}">${result.total_score || 0}</h2>
                                                    <p class="mb-2">总评分 (满分100)</p>
                                                    <span class="badge bg-${result.lead_score === 'hot' ? 'danger' : result.lead_score === 'warm' ? 'warning' : 'info'} fs-6">${result.lead_score === 'hot' ? '🔥 热线索' : result.lead_score === 'warm' ? '温线索' : '冷线索'}</span>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                    <hr>
                                    <div class="row">
                                        <div class="col-md-6">
                                            <h6>检测到的购买信号</h6>
                                            ${formatSignalsList(result.signals_detected || result.signals || [])}
                                        </div>
                                        <div class="col-md-6">
                                            <h6>购买概率</h6>
                                            <div class="progress" style="height: 25px;">
                                                <div class="progress-bar bg-success" style="width: ${sanitizePercent((result.purchase_probability || 0) * 100)}%">${((result.purchase_probability || 0) * 100).toFixed(0)}%</div>
                                            </div>
                                            <p class="mt-2 small text-muted">预估成交金额: ¥${formatNumber(result.estimated_deal_size || 0)}</p>
                                        </div>
                                    </div>
                                    <hr>
                                    <h6>建议行动</h6>
                                    <p>${escapeHtml(result.recommended_action || result.suggested_action || '继续跟进客户，了解更多需求')}</p>
                                </div>
                                <div class="modal-footer">
                                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
                                    <button type="button" class="btn btn-primary" data-customer-name="${escapeHtml(customerName)}" onclick="viewCustomerDetail(this.dataset.customerName); bootstrap.Modal.getInstance(document.getElementById('intentAnalysisModal')).hide();">查看客户</button>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
                
                // 移除旧模态框
                const oldModal = document.getElementById('intentAnalysisModal');
                if (oldModal) oldModal.remove();
                
                // 添加新模态框
                document.body.insertAdjacentHTML('beforeend', modalHtml);
                
                // 显示模态框
                const modalElement = bindModalAccessibility(
                    document.getElementById('intentAnalysisModal'),
                    { removeOnHidden: true }
                );
                const modal = new bootstrap.Modal(modalElement);
                modal.show();
            }
        } else {
            showToast('分析失败，请稍后重试', 'error');
        }
    } catch (e) {
        showToast('分析失败: ' + e.message, 'error');
    }
}

/**
 * 格式化信号列表
 */
function formatSignalsList(signals) {
    if (!signals || signals.length === 0) {
        return '<p class="text-muted">暂未检测到购买信号</p>';
    }
    
    const signalLabels = {
        'explicit_intent': { label: '明确意向', icon: '🎯', color: 'success' },
        'price_inquiry': { label: '询价', icon: '💰', color: 'warning' },
        'demo_request': { label: '请求演示', icon: '🖥️', color: 'info' },
        'contact_request': { label: '请求联系', icon: '📞', color: 'primary' },
        'budget_discussion': { label: '预算讨论', icon: '💵', color: 'success' },
        'decision_maker': { label: '决策者', icon: '👔', color: 'danger' },
        'timeline_mention': { label: '时间线提及', icon: '📅', color: 'info' },
        'competitor_comparison': { label: '竞品对比', icon: '⚖️', color: 'warning' },
        'feature_inquiry': { label: '功能咨询', icon: '🔧', color: 'info' },
        'objection': { label: '异议表达', icon: '❓', color: 'secondary' }
    };
    
    return signals.map(signal => {
        const info = signalLabels[signal] || { label: signal, icon: '📌', color: 'secondary' };
        return `<span class="badge bg-${info.color} me-1 mb-1">${info.icon} ${info.label}</span>`;
    }).join('');
}

// 更新待办事项
function updateActionItems(items) {
    const container = document.getElementById('action-items-list');
    const countBadge = document.getElementById('action-count');
    
    if (!container) return;

    const visibleItems = items || [];

    if (countBadge) {
        countBadge.textContent = visibleItems.length;
    }
    
    if (!visibleItems || visibleItems.length === 0) {
        container.innerHTML = `
            <div class="text-center text-muted py-4">
                <div class="font-size-32">✅</div>
                <div class="mt-2">当前暂无高意向待跟进客户</div>
                <small>只有高意向或已留资客户才会显示在这里</small>
            </div>
        `;
        return;
    }
    
    container.innerHTML = '';
    visibleItems.forEach((item, index) => {
        const priorityClass = item.priority === 'urgent' ? 'danger' : item.priority === 'high' ? 'warning' : item.priority === 'medium' ? 'info' : 'secondary';
        const priorityLabel = item.priority === 'urgent' ? '🚨 紧急' : item.priority === 'high' ? '⚡ 高' : item.priority === 'medium' ? '📌 中' : '📋 低';
        
        const typeLabels = {
            'contact_provided_follow_up': '📲 已留资跟进',
            'hot_lead': '🔥 热线索跟进',
            'hot_lead_follow_up': '🔥 热线索跟进',
            'high_intent': '⭐ 高意向跟进',
            'high_intent_follow_up': '⭐ 高意向跟进',
            'follow_up': '📞 待跟进',
            'risk': '⚠️ 风险处理',
            'nurture': '🌱 培育客户',
            'churn_risk': '🚨 流失风险'
        };
        const typeLabel = typeLabels[item.type] || '📋 待跟进';
        
        const customerName = item.customer_name || item.customer_id || '未知客户';
        const estimatedValue = item.estimated_value || 0;
        const contactInfo = item.contact_info || '';
        const suggestedAction = item.suggested_action || item.description || '';
        const conversationId = item.conversation_id || '';
        const notificationSignature = item.notification_signature || '';
        
        const card = document.createElement('div');
        card.className = `card mb-2 border-${priorityClass} action-item`;
        card.dataset.index = index;
        card.dataset.customerName = customerName;
        card.dataset.itemType = item.type || 'follow_up';
        card.dataset.conversationId = conversationId;
        card.dataset.signature = notificationSignature;
        
        const cardBody = document.createElement('div');
        cardBody.className = 'card-body py-2 px-3';
        
        const headerDiv = document.createElement('div');
        headerDiv.className = 'd-flex justify-content-between align-items-center mb-1';
        headerDiv.innerHTML = `<span class="badge bg-${priorityClass}">${priorityLabel}</span>`;
        const typeSmall = document.createElement('small');
        typeSmall.className = 'text-muted';
        typeSmall.textContent = typeLabel;
        headerDiv.appendChild(typeSmall);
        cardBody.appendChild(headerDiv);
        
        const nameDiv = document.createElement('div');
        nameDiv.className = 'd-flex align-items-center mb-1';
        const nameStrong = document.createElement('strong');
        nameStrong.className = 'me-2';
        nameStrong.textContent = customerName;
        nameDiv.appendChild(nameStrong);
        if (estimatedValue > 0) {
            const valueBadge = document.createElement('span');
            valueBadge.className = 'badge bg-success';
            valueBadge.textContent = `¥${formatNumber(estimatedValue)}`;
            nameDiv.appendChild(valueBadge);
        }
        cardBody.appendChild(nameDiv);
        
        const descSmall = document.createElement('small');
        descSmall.className = 'text-muted d-block mb-2';
        descSmall.textContent = item.description || '';
        cardBody.appendChild(descSmall);

        if (contactInfo) {
            const contactSmall = document.createElement('small');
            contactSmall.className = 'text-success d-block mb-2';
            contactSmall.textContent = `已留资: ${contactInfo}`;
            cardBody.appendChild(contactSmall);
        }
        
        if (suggestedAction) {
            const suggestSmall = document.createElement('small');
            suggestSmall.className = 'text-primary d-block mb-2';
            suggestSmall.textContent = `💡 建议: ${suggestedAction.substring(0, 50)}${suggestedAction.length > 50 ? '...' : ''}`;
            cardBody.appendChild(suggestSmall);
        }

        const dingTalkStatusMeta = getDingTalkNotificationStatusMeta(item);
        if (dingTalkStatusMeta) {
            const dingTalkSmall = document.createElement('small');
            dingTalkSmall.className = `d-block mb-2 ${dingTalkStatusMeta.className}`;
            dingTalkSmall.textContent = dingTalkStatusMeta.text;
            cardBody.appendChild(dingTalkSmall);
        }
        
        const btnGroup = document.createElement('div');
        btnGroup.className = 'btn-group btn-group-sm w-100';
        const itemType = item.type || 'follow_up';
        
        const followBtn = document.createElement('button');
        followBtn.className = 'btn btn-primary';
        followBtn.textContent = '立即跟进';
        followBtn.addEventListener('click', () => handleActionItem(item, 'follow'));
        btnGroup.appendChild(followBtn);
        
        const detailBtn = document.createElement('button');
        detailBtn.className = 'btn btn-outline-secondary';
        detailBtn.textContent = '查看详情';
        detailBtn.addEventListener('click', () => handleActionItem(item, 'detail'));
        btnGroup.appendChild(detailBtn);
        
        const completeBtn = document.createElement('button');
        completeBtn.className = 'btn btn-outline-success';
        completeBtn.textContent = '标记已跟进';
        completeBtn.title = '标记完成';
        completeBtn.addEventListener('click', () => handleActionItem(item, 'complete'));
        btnGroup.appendChild(completeBtn);
        
        cardBody.appendChild(btnGroup);
        card.appendChild(cardBody);
        container.appendChild(card);
    });
}

/**
 * 处理待办事项操作
 */
function renderFollowUpDetailModal(item = {}) {
    const customerName = item.customer_name || item.customer_id || '未知客户';
    const profileUrl = item.profile_url || '';
    const sourceVideoUrl = getPreferredCustomerVideoUrl(item);
    const contactInfo = item.contact_info || '';
    const contactSourceMessage = item.contact_source_message || item.latest_inbound_message || '';
    const latestMessagePreview = item.latest_message_preview || '';
    const tags = Array.isArray(item.tags) ? item.tags : [];
    const priorityClass = item.priority === 'urgent' ? 'danger' : item.priority === 'high' ? 'warning' : item.priority === 'medium' ? 'info' : 'secondary';
    const priorityLabel = item.priority === 'urgent' ? '紧急' : item.priority === 'high' ? '高' : item.priority === 'medium' ? '中' : '低';
    const typeLabels = {
        'contact_provided_follow_up': '已留资跟进',
        'hot_lead': '热线索跟进',
        'hot_lead_follow_up': '热线索跟进',
        'high_intent': '高意向跟进',
        'high_intent_follow_up': '高意向跟进',
        'follow_up': '待跟进',
        'risk': '风险处理',
        'nurture': '培育客户',
        'churn_risk': '流失风险'
    };
    const typeLabel = typeLabels[item.type] || '待跟进';

    return `
        <div class="modal fade" id="followUpDetailModal" tabindex="-1">
            <div class="modal-dialog modal-lg modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">待跟进详情 - ${escapeHtml(customerName)}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <div class="row g-3 mb-3">
                            <div class="col-md-6">
                                <div class="border rounded p-3 bg-light h-100">
                                    <div class="small text-muted mb-1">跟进概况</div>
                                    <div class="mb-2">
                                        <span class="badge bg-${priorityClass} me-2">${priorityLabel}</span>
                                        <span class="badge bg-secondary">${escapeHtml(typeLabel)}</span>
                                    </div>
                                    <div class="small mb-1">客户：${escapeHtml(customerName)}</div>
                                    <div class="small mb-1">平台：${escapeHtml(item.platform || 'douyin')}</div>
                                    <div class="small mb-1">客户ID：${escapeHtml(item.sec_uid || item.customer_id || '-')}</div>
                                    <div class="small mb-1">唯一号：${escapeHtml(item.unique_id || '-')}</div>
                                    <div class="small mb-1">最近消息时间：${escapeHtml(formatAnalyticsTime(item.last_message_time))}</div>
                                    <div class="small mb-0">意向等级：${escapeHtml(item.intent_level || '-')} / 分数：${escapeHtml(String(item.intent_score ?? item.score ?? '-'))}</div>
                                </div>
                            </div>
                            <div class="col-md-6">
                                <div class="border rounded p-3 bg-light h-100">
                                    <div class="small text-muted mb-1">留资信息</div>
                                    ${contactInfo
                                        ? `<div class="fw-semibold text-success mb-2">${escapeHtml(contactInfo)}</div>`
                                        : '<div class="text-muted mb-2">当前未识别到明确留资信息</div>'}
                                    <div class="small text-muted mb-1">留资来源</div>
                                    <div class="small">${escapeHtml(contactSourceMessage || '暂无留资原文')}</div>
                                </div>
                            </div>
                        </div>

                        <div class="mb-3">
                            <h6>建议动作</h6>
                            <div class="border rounded p-3 bg-light">${escapeHtml(item.suggested_action || item.description || '暂无建议')}</div>
                        </div>

                        <div class="mb-3">
                            <h6>跟进说明</h6>
                            <div class="border rounded p-3 bg-light">${escapeHtml(item.description || '暂无说明')}</div>
                        </div>

                        <div class="mb-3">
                            <h6>最近互动摘要</h6>
                            <div class="border rounded p-3 bg-light">${escapeHtml(latestMessagePreview || '暂无最近消息')}</div>
                        </div>

                        <div class="mb-3">
                            <h6>客户标签</h6>
                            <div>${renderTagBadges(tags, 'bg-secondary', '暂无标签')}</div>
                        </div>

                        ${item.comment_content ? `
                            <div class="mb-3">
                                <h6>评论内容</h6>
                                <div class="border rounded p-3 bg-light">${escapeHtml(item.comment_content)}</div>
                            </div>
                        ` : ''}
                    </div>
                    <div class="modal-footer">
                        ${profileUrl ? `<a class="btn btn-primary" href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer">打开抖音主页</a>` : ''}
                        ${sourceVideoUrl ? `<a class="btn btn-outline-secondary" href="${escapeHtml(sourceVideoUrl)}" target="_blank" rel="noopener noreferrer">打开来源视频</a>` : ''}
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function showFollowUpDetailModal(item = {}) {
    const oldModal = document.getElementById('followUpDetailModal');
    if (oldModal) oldModal.remove();
    document.body.insertAdjacentHTML('beforeend', renderFollowUpDetailModal(item));
    const modalElement = bindModalAccessibility(
        document.getElementById('followUpDetailModal'),
        { removeOnHidden: true }
    );
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
}

async function handleActionItem(item, action) {
    const customerName = item?.customer_name || item?.customer_id || '未知客户';
    const conversationId = item?.conversation_id || '';
    const signature = item?.notification_signature || '';
    switch (action) {
        case 'follow':
            {
                await openFollowUpInDouyinBrowser(customerName);
            }
            break;
            
        case 'detail':
            showFollowUpDetailModal(item || {});
            break;
            
        case 'complete':
            {
                if (!conversationId || !signature) {
                    showToast('缺少待跟进标识，无法标记完成', 'warning');
                    break;
                }

                try {
                    const response = await fetch('/api/enhanced-analytics/follow-up/complete', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            conversation_id: conversationId,
                            signature
                        })
                    });
                    const result = await response.json();
                    if (!response.ok || result.success === false) {
                        throw new Error(result.message || '标记失败');
                    }
                    showToast(`已标记 ${customerName} 为已跟进`, 'success');
                    loadEnhancedAnalytics({ refreshAdvancedScoring: false });
                } catch (error) {
                    showToast(`标记失败: ${error.message}`, 'danger');
                }
            }
            break;
    }
}

// 格式化数字
function formatNumber(num) {
    if (num >= 10000) {
        return (num / 10000).toFixed(1) + '万';
    }
    return num.toLocaleString();
}

// 格式化购买信号
function formatSignals(signals) {
    if (!signals || signals.length === 0) return '-';
    const signalLabels = {
        'explicit_intent': '明确意向',
        'price_inquiry': '询价',
        'demo_request': '请求演示',
        'contact_request': '请求联系',
        'budget_discussion': '预算讨论',
        'decision_maker': '决策者'
    };
    return signals.slice(0, 2).map(s => `<span class="badge bg-secondary me-1">${signalLabels[s] || escapeHtml(String(s))}</span>`).join('');
}

// 格式化最后联系时间
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
    } catch {
        return time;
    }
}

// ========== WebSocket实时更新 ==========

let websocket = null;
let websocketReconnectTimer = null;

/**
 * 连接WebSocket
 */
function connectWebSocket() {
    if (websocket && websocket.readyState === WebSocket.OPEN) {
        return;
    }
    
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/intent-updates`;
    
    try {
        websocket = new WebSocket(wsUrl);
        
        websocket.onopen = () => {
            console.log('WebSocket连接已建立');
            showToast('实时更新已连接', 'success');
            websocketReconnectAttempt = 0;
            
            if (websocketReconnectTimer) {
                clearTimeout(websocketReconnectTimer);
                websocketReconnectTimer = null;
            }
        };
        
        websocket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                handleWebSocketMessage(data);
            } catch (e) {
                console.error('解析WebSocket消息失败', e);
            }
        };
        
        websocket.onclose = () => {
            console.log('WebSocket连接已关闭');
            if (!websocketReconnectTimer) {
                const baseDelay = 5000;
                const maxDelay = 60000;
                const attempt = websocketReconnectAttempt || 0;
                websocketReconnectAttempt = attempt + 1;
                const delay = Math.min(baseDelay * Math.pow(1.5, attempt), maxDelay);
                websocketReconnectTimer = setTimeout(() => {
                    websocketReconnectTimer = null;
                    connectWebSocket();
                }, delay);
            }
        };
        
        websocket.onerror = (error) => {
            console.error('WebSocket错误', error);
        };
        
    } catch (e) {
        console.error('WebSocket连接失败', e);
    }
}

/**
 * 处理WebSocket消息
 */
function handleWebSocketMessage(data) {
    switch (data.type) {
        case 'initial':
            // 初始数据
            if (data.data) {
                updateRealtimeData(data.data);
            }
            break;
            
        case 'intent_update':
            // 意向更新
            if (data.data) {
                updateRealtimeData(data.data);
                
                // 显示变化通知
                if (data.changes && data.changes.new_high_intent && data.changes.new_high_intent.length > 0) {
                    data.changes.new_high_intent.forEach(change => {
                        showToast(`发现新的高意向客户: ${change.name} (评分: ${change.score})`, 'success');
                    });
                }
            }
            break;
            
        case 'high_intent_alert':
            // 高意向客户警报
            if (data.data) {
                showToast(data.data.message, 'warning');
                // 刷新高意向客户列表
                refreshHighIntentCustomers();
            }
            break;
            
        case 'heartbeat':
            // 心跳，忽略
            break;
            
        default:
            console.log('未知WebSocket消息类型', data.type);
    }
}

/**
 * 更新实时数据
 */
function updateRealtimeData(data) {
    if (!data) return;
    
    if (data.high_intent_customers) {
        updateHighIntentCustomersList(data.high_intent_customers);
    }

    if (data.pending_follow_ups_count !== undefined) {
        const actionCount = document.getElementById('action-count');
        if (actionCount) {
            actionCount.textContent = data.pending_follow_ups_count;
        }
    }
}

/**
 * 发送WebSocket消息
 */
function sendWebSocketMessage(message) {
    if (websocket && websocket.readyState === WebSocket.OPEN) {
        websocket.send(JSON.stringify(message));
    }
}

/**
 * 请求刷新实时数据
 */
function requestRefresh() {
    sendWebSocketMessage({ type: 'refresh' });
}

function renderCustomerDetailModal(customer = {}) {
    const tags = Array.isArray(customer.tags) ? customer.tags : [];
    const nickname = customer.nickname || customer.customer_name || customer.sec_uid || '未知客户';
    const profileUrl = customer.profile_url || '';
    const videoDisplay = getCustomerVideoDisplay(customer);
    const sourceVideoUrl = videoDisplay.sourceVideoUrl || '';
    const avatarUrl = customer.avatar_url || '';
    const commentContent = customer.comment_content || '';
    const signature = customer.signature || '';
    const authorName = customer.author_name || '';
    const ipLocation = customer.ip_location || '';

    return `
        <div class="modal fade" id="customerDetailModal" tabindex="-1">
            <div class="modal-dialog modal-lg modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">客户详情</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <div class="d-flex align-items-start gap-3 mb-3">
                            ${avatarUrl ? `<img src="${escapeHtml(avatarUrl)}" alt="${escapeHtml(nickname)}" class="rounded-circle" width="56" height="56">` : ''}
                            <div class="flex-grow-1">
                                <div class="fw-bold fs-5">${escapeHtml(nickname)}</div>
                                <div class="text-muted small">${escapeHtml(customer.platform || 'douyin')} / ${escapeHtml(customer.unique_id || customer.sec_uid || '-')}</div>
                                <div class="mt-2">
                                    <span class="badge ${getStatusBadge(customer.status)} me-2">${escapeHtml(getStatusLabel(customer.status || 'pending'))}</span>
                                    <span class="badge ${getInteractStatusBadge(customer.interact_status || 'pending')} me-2">${escapeHtml(getInteractStatusLabel(customer.interact_status || 'pending'))}</span>
                                    <span class="badge intent-${escapeHtml(customer.intent_level || 'D')}">${escapeHtml(customer.intent_level || 'D')}级</span>
                                </div>
                            </div>
                        </div>

                        <div class="row g-3">
                            <div class="col-md-6">
                                <h6>基础信息</h6>
                                <table class="table table-sm align-middle">
                                    <tr><td>客户昵称</td><td>${escapeHtml(nickname)}</td></tr>
                                    <tr><td>平台</td><td>${escapeHtml(customer.platform || '-')}</td></tr>
                                    <tr><td>客户ID</td><td class="text-break">${escapeHtml(customer.sec_uid || '-')}</td></tr>
                                    <tr><td>唯一号</td><td>${escapeHtml(customer.unique_id || '-')}</td></tr>
                                    <tr><td>IP归属</td><td>${escapeHtml(ipLocation || '-')}</td></tr>
                                    <tr><td>意向等级</td><td>${escapeHtml(customer.intent_level || '-')}</td></tr>
                                    <tr><td>意向分数</td><td>${escapeHtml(String(customer.intent_score ?? '-'))}</td></tr>
                                </table>
                            </div>
                            <div class="col-md-6">
                                <h6>互动信息</h6>
                                <table class="table table-sm align-middle">
                                    <tr><td>评论时间</td><td>${escapeHtml(formatCustomerCommentTime(customer.comment_time) || '-')}</td></tr>
                                    <tr><td>采集时间</td><td>${escapeHtml(customer.created_at ? new Date(customer.created_at).toLocaleString() : '-')}</td></tr>
                                    <tr><td>更新时间</td><td>${escapeHtml(customer.updated_at ? new Date(customer.updated_at).toLocaleString() : '-')}</td></tr>
                                    <tr><td>来源作者</td><td>${escapeHtml(authorName || '-')}</td></tr>
                                    <tr><td>标签</td><td>${tags.length ? tags.map(tag => `<span class="badge bg-secondary me-1">${escapeHtml(tag)}</span>`).join('') : '<span class="text-muted">暂无</span>'}</td></tr>
                                </table>
                            </div>
                        </div>

                        <div class="mt-3">
                            <h6>评论内容</h6>
                            <div class="border rounded p-3 bg-light">${escapeHtml(commentContent || '暂无评论内容')}</div>
                        </div>

                        <div class="mt-3">
                            <h6>账号简介</h6>
                            <div class="border rounded p-3 bg-light">${escapeHtml(signature || '暂无简介')}</div>
                        </div>

                        <div class="mt-3">
                            <h6>来源视频</h6>
                            <div class="border rounded p-3 bg-light">
                                <div class="mb-2">${escapeHtml(videoDisplay.title || '暂无视频标题')}</div>
                                ${sourceVideoUrl ? `<a href="${escapeHtml(sourceVideoUrl)}" target="_blank" rel="noopener noreferrer">打开来源视频</a>` : '<span class="text-muted">暂无视频链接</span>'}
                            </div>
                        </div>

                        ${profileUrl ? `
                            <div class="mt-3">
                                <a href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer">打开客户主页</a>
                            </div>
                        ` : ''}
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function showCustomerDetailModal(modalHtml) {
    const oldModal = document.getElementById('customerDetailModal');
    if (oldModal) oldModal.remove();
    document.body.insertAdjacentHTML('beforeend', modalHtml);
    const modalElement = bindModalAccessibility(
        document.getElementById('customerDetailModal'),
        { removeOnHidden: true }
    );
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
}

function renderCustomerInsightModal(insight = {}) {
    return `
        <div class="modal fade" id="customerDetailModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">客户洞察 - ${escapeHtml(insight.customer_name || '未知客户')}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <div class="row">
                            <div class="col-md-6">
                                <h6>意向分析</h6>
                                <table class="table table-sm">
                                    <tr><td>意向评分</td><td><strong>${escapeHtml(String(insight.intent?.score ?? '-'))}</strong></td></tr>
                                    <tr><td>线索等级</td><td><span class="badge bg-${insight.intent?.lead_score === 'hot' ? 'danger' : insight.intent?.lead_score === 'warm' ? 'warning' : 'info'}">${escapeHtml(insight.intent?.lead_score || '-')}</span></td></tr>
                                    <tr><td>购买概率</td><td>${((insight.intent?.probability || 0) * 100 || 0).toFixed(0)}%</td></tr>
                                    <tr><td>生命周期阶段</td><td>${escapeHtml(insight.intent?.stage || '-')}</td></tr>
                                    <tr><td>购买角色</td><td>${escapeHtml(insight.intent?.role || '-')}</td></tr>
                                </table>
                            </div>
                            <div class="col-md-6">
                                <h6>价值评估</h6>
                                <table class="table table-sm">
                                    <tr><td>预估价值</td><td>¥${formatNumber(insight.value?.estimated)}</td></tr>
                                    <tr><td>生命周期价值</td><td>¥${formatNumber(insight.value?.lifetime)}</td></tr>
                                    <tr><td>活跃度</td><td>${escapeHtml(insight.behavior?.engagement || '-')}</td></tr>
                                    <tr><td>响应率</td><td>${((insight.behavior?.response_rate || 0) * 100 || 0).toFixed(0)}%</td></tr>
                                </table>
                            </div>
                        </div>
                        <hr>
                        <h6>行为特征</h6>
                        <p><strong>标签:</strong> ${((insight.behavior?.tags) || []).map(t => `<span class="badge bg-secondary me-1">${escapeHtml(t)}</span>`).join('') || '暂无'}</p>
                        <p><strong>兴趣:</strong> ${escapeHtml(((insight.behavior?.interests) || []).join(', ')) || '暂无'}</p>
                        <p><strong>购买信号:</strong> ${formatSignals(insight.behavior?.signals)}</p>
                        <hr>
                        <h6>推荐行动</h6>
                        <p>${escapeHtml(insight.recommendations?.next_action || '暂无')}</p>
                        <p class="text-muted">${escapeHtml(insight.recommendations?.follow_up || '')}</p>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

// 查看客户详情
async function viewCustomerDetail(customerIdOrName, platform = 'douyin', customerName = '') {
    try {
        if (customerIdOrName) {
            const detailRes = await fetchWithTimeout(
                `${API_BASE}/customers/${encodeURIComponent(customerIdOrName)}?platform=${encodeURIComponent(platform || 'douyin')}`
            );
            if (detailRes.ok) {
                const customer = await detailRes.json();
                showCustomerDetailModal(renderCustomerDetailModal(customer));
                return;
            }
        }

        const fallbackName = customerName || customerIdOrName;
        const res = await fetchWithTimeout(`${API_BASE}/enhanced-analytics/customer/${encodeURIComponent(fallbackName)}`);
        if (!res.ok) {
            throw new Error('客户详情不存在');
        }

        const data = await res.json();
        if (!data.success || !data.data) {
            throw new Error(data.message || '客户详情不存在');
        }

        showCustomerDetailModal(renderCustomerInsightModal(data.data));
    } catch (e) {
        showToast('获取客户详情失败: ' + e.message, 'error');
    }
}

// 测试智能回复
async function testSmartReply() {
    const customerName = document.getElementById('test-customer-name').value.trim() || '客户';
    const message = document.getElementById('test-message').value.trim();
    const enterpriseId = document.getElementById('test-enterprise').value;
    
    if (!message) {
        alert('请输入测试消息');
        return;
    }
    
    try {
        const res = await fetchWithTimeout(`${API_BASE}/smart-reply`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                customer_name: customerName,
                enterprise_id: enterpriseId
            })
        });
        
        const result = await res.json();
        
        // 显示结果
        document.getElementById('test-result').classList.remove('d-none');
        document.getElementById('test-reply-content').textContent = result.reply;
        
        const levelBadge = document.getElementById('test-intent-level');
        levelBadge.textContent = result.intent_level + '级';
        levelBadge.className = `badge intent-${result.intent_level}`;
        
        document.getElementById('test-intent-score').textContent = result.intent_score;
        document.getElementById('test-matched-knowledge').textContent = result.matched_knowledge || '无';
        document.getElementById('test-suggested-action').textContent = result.suggested_action || '无';
        
    } catch (e) {
        alert('测试失败: ' + e.message);
    }
}

// ========== RAG搜索功能 ==========

/**
 * RAG搜索测试
 */
async function testRAGSearch() {
    const message = document.getElementById('test-message').value.trim();
    const enterpriseId = document.getElementById('test-enterprise').value;
    
    if (!message) {
        alert('请输入测试消息');
        return;
    }
    
    try {
        const res = await fetchWithTimeout(`${API_BASE}/rag/search`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                query: message,
                enterprise_id: enterpriseId,
                top_k: 5
            })
        });
        
        const result = await res.json();
        
        // 显示RAG结果
        document.getElementById('rag-result').classList.remove('d-none');
        document.getElementById('rag-result-count').textContent = result.total || 0;
        
        const content = document.getElementById('rag-result-content');
        content.innerHTML = '';
        
        if (result.results && result.results.length > 0) {
            result.results.forEach((item, index) => {
                const div = document.createElement('div');
                div.className = 'mb-2 p-2 border rounded';
                div.innerHTML = `
                    <div class="d-flex justify-content-between">
                        <strong>${index + 1}. ${escapeHtml(item.question || item.title || '文档片段')}</strong>
                        <span class="badge bg-info">${(item.score * 100).toFixed(1)}%</span>
                    </div>
                    <p class="mb-1 small text-muted">${escapeHtml((item.answer || item.content || '').substring(0, 150))}...</p>
                    <small class="text-muted">来源: ${escapeHtml(item.source || '知识库')}</small>
                `;
                content.appendChild(div);
            });
        } else {
            content.innerHTML = '<p class="text-muted">未找到相关内容</p>';
        }
        
    } catch (e) {
        showToast('RAG搜索失败: ' + e.message, 'error');
    }
}

/**
 * 意图识别测试
 */
async function testIntentRecognize() {
    const message = document.getElementById('test-message').value.trim();
    const enterpriseId = document.getElementById('test-enterprise').value;
    
    if (!message) {
        alert('请输入测试消息');
        return;
    }
    
    try {
        const res = await fetchWithTimeout(`${API_BASE}/intent/recognize`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: message,
                enterprise_id: enterpriseId
            })
        });
        
        const result = await res.json();
        
        // 显示意图识别结果
        document.getElementById('intent-result').classList.remove('d-none');
        document.getElementById('intent-confidence').textContent = (result.confidence * 100).toFixed(1) + '%';
        
        // 主要意图
        const primaryIntent = document.getElementById('intent-primary');
        const intentLabels = {
            'product_inquiry': '产品咨询',
            'price_inquiry': '价格咨询',
            'service_inquiry': '服务咨询',
            'cooperation_intent': '合作意向',
            'purchase_intent': '购买意向',
            'complaint': '投诉',
            'consultation': '咨询',
            'comparison': '对比',
            'feedback': '反馈',
            'greeting': '问候',
            'farewell': '告别',
            'thanks': '感谢',
            'confirmation': '确认',
            'rejection': '拒绝',
            'unknown': '未知'
        };
        primaryIntent.textContent = intentLabels[result.primary_intent] || result.primary_intent;
        
        // 次要意图
        const secondaryIntents = (result.secondary_intents || []).map(i => 
            `<span class="badge bg-secondary me-1">${escapeHtml(intentLabels[i] || i)}</span>`
        ).join('');
        document.getElementById('intent-secondary').innerHTML = secondaryIntents || '<span class="text-muted">无</span>';
        
        // 情感分析
        const sentimentEl = document.getElementById('intent-sentiment');
        const sentimentLabels = {
            'positive': { text: '积极', class: 'bg-success' },
            'negative': { text: '消极', class: 'bg-danger' },
            'neutral': { text: '中性', class: 'bg-secondary' },
            'mixed': { text: '混合', class: 'bg-warning' }
        };
        const sentiment = sentimentLabels[result.sentiment] || { text: result.sentiment, class: 'bg-secondary' };
        sentimentEl.textContent = sentiment.text;
        sentimentEl.className = `badge ${sentiment.class}`;
        
        // 紧急程度
        const urgencyEl = document.getElementById('intent-urgency');
        const urgencyLabels = {
            'high': { text: '高', class: 'bg-danger' },
            'medium': { text: '中', class: 'bg-warning' },
            'low': { text: '低', class: 'bg-info' }
        };
        const urgency = urgencyLabels[result.urgency] || { text: result.urgency, class: 'bg-secondary' };
        urgencyEl.textContent = urgency.text;
        urgencyEl.className = `badge ${urgency.class}`;
        
        // 关键词
        const keywordsEl = document.getElementById('intent-keywords');
        keywordsEl.innerHTML = (result.keywords || []).map(kw => 
            `<span class="badge bg-info me-1">${escapeHtml(kw)}</span>`
        ).join('') || '<span class="text-muted">无</span>';
        
        // 实体
        const entitiesEl = document.getElementById('intent-entities');
        if (result.entities && Object.keys(result.entities).length > 0) {
            entitiesEl.innerHTML = Object.entries(result.entities).map(([type, values]) => 
                `<div><small class="text-muted">${type}:</small> ${values.map(v => `<span class="badge bg-light text-dark me-1">${escapeHtml(v)}</span>`).join('')}</div>`
            ).join('');
        } else {
            entitiesEl.innerHTML = '<span class="text-muted">无</span>';
        }
        
        // 是否需要人工
        const needHumanEl = document.getElementById('intent-need-human');
        needHumanEl.textContent = result.need_human ? '是' : '否';
        needHumanEl.className = `badge ${result.need_human ? 'bg-warning' : 'bg-success'}`;
        
        // 推理过程
        document.getElementById('intent-reasoning').textContent = result.reasoning || '无';
        
    } catch (e) {
        showToast('意图识别失败: ' + e.message, 'error');
    }
}

// ========== 企业管理功能 ==========

let enterpriseModal;

/**
 * 加载企业列表
 */
async function loadEnterprises() {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/enterprises`);
        const enterprises = await res.json();
        
        // 更新所有企业选择器
        const selectors = [
            'filter-enterprise', 'test-enterprise', 'knowledge-enterprise'
        ];
        
        selectors.forEach(id => {
            const select = document.getElementById(id);
            if (select) {
                const currentValue = select.value;
                select.innerHTML = id.includes('filter') || id.includes('graph') 
                    ? '<option value="">全部企业</option>' 
                    : '<option value="">默认企业</option>';
                enterprises.forEach(ent => {
                    select.innerHTML += `<option value="${escapeHtml(String(ent.id))}">${escapeHtml(ent.name)}</option>`;
                });
                if (currentValue) select.value = currentValue;
            }
        });
        
    } catch (e) {
        console.error("Load enterprises failed", e);
    }
}

/**
 * 显示添加企业模态框
 */
function showAddEnterpriseModal() {
    document.getElementById('enterpriseModalTitle').textContent = '添加企业';
    document.getElementById('enterprise-form').reset();
    document.getElementById('enterprise-id').value = '';
    document.getElementById('enterprise-enabled').checked = true;
    enterpriseModal = new bootstrap.Modal(document.getElementById('enterpriseModal'));
    enterpriseModal.show();
}

/**
 * 保存企业
 */
async function saveEnterprise() {
    const id = document.getElementById('enterprise-id').value;
    const name = document.getElementById('enterprise-name').value.trim();
    const code = document.getElementById('enterprise-code').value.trim();
    
    if (!name || !code) {
        alert('请填写企业名称和代码');
        return;
    }
    
    const data = {
        name,
        code,
        contact: document.getElementById('enterprise-contact').value.trim(),
        phone: document.getElementById('enterprise-phone').value.trim(),
        config: document.getElementById('enterprise-config').value.trim(),
        enabled: document.getElementById('enterprise-enabled').checked
    };
    
    try {
        let res;
        if (id) {
            res = await fetchWithTimeout(`${API_BASE}/enterprises/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        } else {
            res = await fetchWithTimeout(`${API_BASE}/enterprises`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        }
        
        const result = await res.json();
        if (res.ok) {
            enterpriseModal.hide();
            loadEnterprises();
            showToast('保存成功', 'success');
        } else {
            alert('保存失败: ' + (result.message || '未知错误'));
        }
    } catch (e) {
        alert('保存失败: ' + e.message);
    }
}

// ========== 文件上传功能 ==========

/**
 * 初始化文件上传拖拽区域
 */
function initFileUpload() {
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    
    if (!dropZone || !fileInput) return;
    
    // 点击上传
    dropZone.addEventListener('click', () => fileInput.click());
    
    // 拖拽事件
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('border-primary');
    });
    
    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('border-primary');
    });
    
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('border-primary');
        const files = e.dataTransfer.files;
        handleFileUpload(files);
    });
    
    // 文件选择
    fileInput.addEventListener('change', (e) => {
        handleFileUpload(e.target.files);
    });
}

/**
 * 处理文件上传
 */
async function handleFileUpload(files) {
    const enterpriseId = 'default';

    const maxSize = 50 * 1024 * 1024;
    const allowedExtensions = new Set(['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.txt', '.csv', '.md', '.json']);
    const progressList = document.getElementById('upload-progress-list') || document.getElementById('upload-progress-list-main');
    
    for (const file of files) {
        const fileExt = '.' + (file.name.split('.').pop() || '').toLowerCase();
        const fileId = `upload-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

        if (!allowedExtensions.has(fileExt)) {
            showToast(`不支持的文件类型: ${file.name}`, 'error');
            continue;
        }
        if (file.size > maxSize) {
            showToast(`文件 ${file.name} 超过50MB限制`, 'error');
            continue;
        }
        
        // 显示进度
        const progressItem = document.createElement('div');
        progressItem.id = fileId;
        progressItem.className = 'progress-item mb-2';
        progressItem.innerHTML = `
            <div class="d-flex justify-content-between mb-1">
                <span>${escapeHtml(file.name)}</span>
                <span class="upload-status text-muted">上传中...</span>
            </div>
            <div class="progress" style="height: 5px;">
                <div class="progress-bar" style="width: 0%"></div>
            </div>
        `;
        progressList.appendChild(progressItem);
        
        try {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('enterprise_id', enterpriseId);
            
            const xhr = new XMLHttpRequest();
            xhr.timeout = 300000;
            
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) {
                    const percent = Math.round((e.loaded / e.total) * 100);
                    progressItem.querySelector('.progress-bar').style.width = percent + '%';
                }
            };
            
            xhr.onload = () => {
                if (xhr.status === 200) {
                    try {
                        const result = JSON.parse(xhr.responseText);
                        if (result.success === false) {
                            progressItem.querySelector('.upload-status').textContent = result.message || '失败';
                            progressItem.querySelector('.upload-status').className = 'upload-status text-danger';
                            return;
                        }
                        progressItem.querySelector('.upload-status').textContent = '完成';
                        progressItem.querySelector('.upload-status').className = 'upload-status text-success';
                        loadUploadedFiles();
                        if (window.KnowledgeApp) {
                            window.KnowledgeApp.loadKnowledge();
                            window.KnowledgeApp.loadStatistics();
                        }
                    } catch (e) {
                        progressItem.querySelector('.upload-status').textContent = '解析响应失败';
                        progressItem.querySelector('.upload-status').className = 'upload-status text-danger';
                    }
                } else {
                    let errorMsg = '失败';
                    try {
                        const errData = JSON.parse(xhr.responseText);
                        errorMsg = errData.message || '失败';
                    } catch (e) {}
                    progressItem.querySelector('.upload-status').textContent = errorMsg;
                    progressItem.querySelector('.upload-status').className = 'upload-status text-danger';
                }
            };
            
            xhr.ontimeout = () => {
                progressItem.querySelector('.upload-status').textContent = '上传超时';
                progressItem.querySelector('.upload-status').className = 'upload-status text-danger';
            };
            
            xhr.onerror = () => {
                progressItem.querySelector('.upload-status').textContent = '失败';
                progressItem.querySelector('.upload-status').className = 'upload-status text-danger';
            };
            
            xhr.open('POST', `${API_BASE}/rag/upload`);
            xhr.send(formData);
            
        } catch (e) {
            progressItem.querySelector('.upload-status').textContent = '失败: ' + e.message;
            progressItem.querySelector('.upload-status').className = 'upload-status text-danger';
        }
    }
}

/**
 * 加载已上传文件列表
 */
async function loadUploadedFiles() {
    try {
        // 获取筛选条件
        const filterEnterprise = document.getElementById('filter-file-enterprise');
        const filterStatus = document.getElementById('filter-file-status');
        ensurePendingOcrStatusOption(filterStatus);
        const enterpriseId = filterEnterprise ? filterEnterprise.value : '';
        const statusFilter = filterStatus ? filterStatus.value : '';
        
        // 构建URL
        let url = `${API_BASE}/rag/files`;
        const params = [];
        if (enterpriseId) params.push(`enterprise_id=${enterpriseId}`);
        if (statusFilter) params.push(`status=${statusFilter}`);
        if (params.length > 0) url += '?' + params.join('&');
            
        const res = await fetchWithTimeout(url);
        const result = await res.json();
        
        const tbody = document.getElementById('uploaded-files-list');
        tbody.innerHTML = '';
        
        // 分类显示名称映射
        const categoryNames = {
            'generic': '📌 通用知识',
            'product': '📦 产品介绍',
            'service': '💬 服务沟通',
            'solution': '🧩 解决方案',
            'policy': '📋 规则政策',
            'process': '🪜 流程说明',
            'faq': '❓ 常见问题',
            'company': '🏢 公司介绍',
            'course': '🎓 课程介绍',
            'attractions': '🏛️ 场景介绍',
            'attraction': '🏛️ 场景介绍',
            'food': '🍜 美食推荐',
            'transport': '🚇 交通出行',
            'accommodation': '🏨 住宿推荐',
            'hotel': '🏨 住宿推荐',
            'itinerary': '📅 行程规划',
            'price': '💰 价格费用',
            'tips': '📝 注意事项',
            'marketing': '📢 营销推广',
            'b2b': '🏢 B2B获客',
            'after_sale': '🔧 售后服务',
            'cooperation': '🤝 合作加盟',
            'promotion': '🎉 促销活动',
            'other': '📁 其他',
            'test': '🧪 测试',
            'general': '📌 通用'
        };
        
        // 状态显示映射
        const statusLabels = {
            'completed': { text: '已完成', class: 'bg-success' },
            'processed': { text: '已完成', class: 'bg-success' },
            'processing': { text: '处理中', class: 'bg-warning' },
            'uploaded': { text: '待处理', class: 'bg-info' },
            'pending_ocr': { text: '待OCR', class: 'bg-warning text-dark' },
            'failed': { text: '失败', class: 'bg-danger' },
            'cancelled': { text: '已取消', class: 'bg-secondary' },
            'timeout': { text: '超时', class: 'bg-secondary' }
        };
        
        let totalSize = 0;
        let totalChunks = 0;
        let totalVectors = 0;
        let hasProcessing = false;
        
        if (result.files && result.files.length > 0) {
            result.files.forEach(file => {
                totalSize += file.size || 0;
                totalChunks += file.chunk_count || 0;
                totalVectors += file.vector_count || 0;
                
                if (file.status === 'processing') hasProcessing = true;
                
                const statusInfo = statusLabels[file.status] || { text: escapeHtml(file.status), class: 'bg-secondary' };
                const categoryName = categoryNames[file.category] || escapeHtml(file.category);
                const statusText = file.status_label || statusInfo.text;
                const statusClass = file.status_badge_class || statusInfo.class;
                const canRetry = typeof file.can_retry_processing === 'boolean'
                    ? file.can_retry_processing
                    : ['uploaded', 'failed', 'timeout', 'cancelled', 'pending_ocr'].includes(file.status);
                
                let statusHtml = `<span class="badge ${statusClass}">${escapeHtml(statusText)}</span>`;
                if ((file.status === 'failed' || file.status === 'pending_ocr') && file.error_message) {
                    statusHtml += `<br><small class="text-danger" title="${escapeHtml(file.error_message)}">${escapeHtml(file.error_message.substring(0, 30))}${file.error_message.length > 30 ? '...' : ''}</small>`;
                } else if (file.status_hint) {
                    statusHtml += `<br><small class="text-muted">${escapeHtml(file.status_hint)}</small>`;
                }
                
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td><input type="checkbox" class="uploaded-file-checkbox" value="${escapeHtml(String(file.id))}" data-enterprise="${escapeHtml(file.enterprise_id || '')}" onchange="updateUploadedFileSelectAllState()"></td>
                    <td title="${escapeHtml(file.filename)}"><span class="text-truncate d-inline-block" style="max-width: 200px;">${escapeHtml(file.filename)}</span></td>
                    <td>${formatFileSize(file.size)}</td>
                    <td><span class="badge bg-secondary">${categoryName}</span></td>
                    <td>${file.chunk_count || 0}</td>
                    <td>${statusHtml}</td>
                    <td><small>${file.upload_time ? new Date(file.upload_time).toLocaleString() : '-'}</small></td>
                    <td>
                        <button class="btn btn-sm btn-outline-info" data-file-id="${escapeHtml(file.id)}" data-enterprise-id="${escapeHtml(file.enterprise_id || '')}" onclick="viewFileDetails(this.dataset.fileId, this.dataset.enterpriseId)" title="详情">📋</button>
                        ${canRetry ? `<button class="btn btn-sm btn-outline-success" data-file-id="${escapeHtml(file.id)}" data-enterprise-id="${escapeHtml(file.enterprise_id || '')}" onclick="reprocessFile(this.dataset.fileId, this.dataset.enterpriseId)" title="重新处理">⚡</button>` : ''}
                        <button class="btn btn-sm btn-outline-danger" data-file-id="${escapeHtml(file.id)}" data-enterprise-id="${escapeHtml(file.enterprise_id || '')}" onclick="deleteUploadedFile(this.dataset.fileId, this.dataset.enterpriseId)" title="删除">🗑️</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }
        
        // 文件处理提示卡片已移除，保留空节点兼容保护，避免旧刷新逻辑报错
        const setOptionalText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        setOptionalText('uploaded-files-count', result.total || 0);
        setOptionalText('total-files-size', formatFileSize(totalSize));
        setOptionalText('total-chunks', totalChunks);
        setOptionalText('total-vectors', totalVectors);
        
        document.getElementById('uploaded-file-select-all').checked = false;
        
        // 更新向量文档统计（同时更新知识库页面和数据分析页面）
        const statVectorCount = document.getElementById('stat-vector-count');
        const statVectorDocs = document.getElementById('stat-vector-docs');
        if (statVectorCount) statVectorCount.textContent = result.total || 0;
        if (statVectorDocs) statVectorDocs.textContent = result.total || 0;
        
        // 更新企业筛选下拉框
        updateEnterpriseFilter(result.files || []);
        
        // 如果有处理中的文件，自动刷新
        if (hasProcessing) {
            if (window._fileRefreshTimer) clearTimeout(window._fileRefreshTimer);
            window._fileRefreshTimer = setTimeout(() => loadUploadedFiles(), 5000);
        } else {
            if (window._fileRefreshTimer) {
                clearTimeout(window._fileRefreshTimer);
                window._fileRefreshTimer = null;
            }
        }
        
    } catch (e) {
        console.error("Load uploaded files failed", e);
    }
}

/**
 * 更新企业筛选下拉框
 */
function updateEnterpriseFilter(files) {
    const filterSelect = document.getElementById('filter-file-enterprise');
    if (!filterSelect) return;
    
    const enterprises = new Set();
    files.forEach(f => {
        if (f.enterprise_id) enterprises.add(f.enterprise_id);
    });
    
    const currentValue = filterSelect.value;
    filterSelect.innerHTML = '<option value="">全部企业</option>';
    enterprises.forEach(ent => {
        const option = document.createElement('option');
        option.value = ent;
        option.textContent = ent;
        filterSelect.appendChild(option);
    });
    filterSelect.value = currentValue;
}

/**
 * 查看文件详情
 */
async function viewFileDetails(fileId, enterpriseId) {
    try {
        let url = `${API_BASE}/enterprise/files/${fileId}`;
        if (enterpriseId) {
            url += `?enterprise_id=${encodeURIComponent(enterpriseId)}`;
        }
        const res = await fetchWithTimeout(url);
        const result = await res.json();
        
        if (!result.success || !result.file) {
            showToast(result.message || '未找到文件信息', 'error');
            return;
        }
        
        const fileInfo = result.file;
        
        const categoryNames = {
            'generic': '📌 通用知识', 'product': '📦 产品介绍', 'service': '💬 服务沟通', 'solution': '🧩 解决方案',
            'policy': '📋 规则政策', 'process': '🪜 流程说明', 'faq': '❓ 常见问题', 'company': '🏢 公司介绍',
            'course': '🎓 课程介绍', 'attractions': '🏛️ 场景介绍', 'attraction': '🏛️ 场景介绍', 'food': '🍜 美食推荐', 'transport': '🚇 交通出行',
            'accommodation': '🏨 住宿推荐', 'hotel': '🏨 住宿推荐', 'itinerary': '📅 行程规划', 'price': '💰 价格费用',
            'tips': '📝 注意事项', 'marketing': '📢 营销推广',
            'b2b': '🏢 B2B获客', 'after_sale': '🔧 售后服务', 'company': '🏢 公司介绍',
            'cooperation': '🤝 合作加盟', 'promotion': '🎉 促销活动',
            'other': '📁 其他', 'test': '🧪 测试', 'general': '📌 通用'
        };
        
        const statusLabels = {
            'completed': '✅ 已完成', 'processed': '✅ 已完成', 'processing': '⏳ 处理中',
            'uploaded': '📋 待处理', 'pending_ocr': '🧾 待OCR', 'failed': '❌ 失败', 'cancelled': '🚫 已取消', 'timeout': '⏰ 超时'
        };
        
        const categoryName = categoryNames[fileInfo.category] || fileInfo.category;
        const statusName = fileInfo.status_label || statusLabels[fileInfo.status] || fileInfo.status;
        
        let detailRows = `
            <tr><th>文件名</th><td>${escapeHtml(fileInfo.filename)}</td></tr>
            <tr><th>大小</th><td>${formatFileSize(fileInfo.size)}</td></tr>
            <tr><th>分类</th><td>${escapeHtml(categoryName)}</td></tr>
            <tr><th>状态</th><td>${escapeHtml(statusName)}</td></tr>
            <tr><th>分块数</th><td>${fileInfo.chunk_count || 0}</td></tr>
            <tr><th>向量数</th><td>${fileInfo.vector_count || 0}</td></tr>
            <tr><th>上传时间</th><td>${fileInfo.upload_time ? new Date(fileInfo.upload_time).toLocaleString() : '-'}</td></tr>
            <tr><th>处理时间</th><td>${fileInfo.processed_time ? new Date(fileInfo.processed_time).toLocaleString() : '-'}</td></tr>
            <tr><th>企业</th><td>${escapeHtml(fileInfo.enterprise_id || '默认')}</td></tr>
        `;
        
        if (fileInfo.error_message) {
            detailRows += `<tr><th>错误信息</th><td class="text-danger">${escapeHtml(fileInfo.error_message)}</td></tr>`;
        }
        
        if (fileInfo.status_hint) {
            detailRows += `<tr><th>状态说明</th><td>${escapeHtml(fileInfo.status_hint)}</td></tr>`;
        }
        
        if (fileInfo.task_id) {
            detailRows += `<tr><th>任务ID</th><td><small>${escapeHtml(fileInfo.task_id)}</small></td></tr>`;
        }
        
        const modalHtml = `
            <div class="modal fade" id="fileDetailModal" tabindex="-1" aria-labelledby="fileDetailModalLabel" aria-hidden="true">
                <div class="modal-dialog">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title" id="fileDetailModalLabel">📄 文件详情</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                        </div>
                        <div class="modal-body">
                            <table class="table table-sm">
                                ${detailRows}
                            </table>
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-sm btn-outline-info" onclick="previewFileContent('${escapeHtml(fileId)}', '${escapeHtml(enterpriseId || '')}')" title="预览内容">📖 预览内容</button>
                            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal" onclick="document.activeElement.blur()">关闭</button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        
        const oldModal = document.getElementById('fileDetailModal');
        if (oldModal) oldModal.remove();
        
        document.body.insertAdjacentHTML('beforeend', modalHtml);
        
        const modalElement = document.getElementById('fileDetailModal');
        
        modalElement.addEventListener('hidden.bs.modal', function () {
            if (document.activeElement) {
                document.activeElement.blur();
            }
            modalElement.remove();
        });
        
        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    } catch (e) {
        showToast('获取文件详情失败: ' + e.message, 'error');
    }
}

async function previewFileContent(fileId, enterpriseId) {
    try {
        const params = enterpriseId ? `?enterprise_id=${encodeURIComponent(enterpriseId)}` : '';
        const res = await fetchWithTimeout(`${API_BASE}/rag/files/${fileId}/content${params}`);
        const data = await res.json();
        
        if (data.success && data.content) {
            const previewHtml = `
                <div class="modal fade" id="filePreviewModal" tabindex="-1" aria-hidden="true">
                    <div class="modal-dialog modal-lg">
                        <div class="modal-content">
                            <div class="modal-header">
                                <h5 class="modal-title">📖 文件内容预览</h5>
                                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                            </div>
                            <div class="modal-body">
                                <pre style="max-height: 500px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; font-size: 12px;">${escapeHtml(data.content.substring(0, 10000))}${data.content.length > 10000 ? '\n\n... (内容过长，仅显示前10000字符)' : ''}</pre>
                            </div>
                            <div class="modal-footer">
                                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
                            </div>
                        </div>
                    </div>
                </div>
            `;
            
            const oldModal = document.getElementById('filePreviewModal');
            if (oldModal) oldModal.remove();
            
            document.body.insertAdjacentHTML('beforeend', previewHtml);
            
            const modalElement = document.getElementById('filePreviewModal');
            modalElement.addEventListener('hidden.bs.modal', function () {
                modalElement.remove();
            });
            
            const modal = new bootstrap.Modal(modalElement);
            modal.show();
        } else {
            showToast('无法获取文件内容', 'warning');
        }
    } catch (e) {
        showToast('获取文件内容失败: ' + e.message, 'error');
    }
}

/**
 * 删除已上传文件
 */
async function deleteUploadedFile(fileId, enterpriseId) {
    if (!confirm('确定要删除这个文件吗？相关的向量数据也会被删除。')) return;
    
    try {
        const url = enterpriseId 
            ? `${API_BASE}/rag/files/${fileId}?enterprise_id=${enterpriseId}` 
            : `${API_BASE}/rag/files/${fileId}`;
        const res = await fetchWithTimeout(url, { method: 'DELETE' });
        if (res.ok) {
            loadUploadedFiles();
            showToast('删除成功', 'success');
        } else {
            const data = await res.json();
            alert('删除失败: ' + (data.message || '未知错误'));
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

/**
 * 全选/取消全选已上传文件
 */
function toggleUploadedFileSelectAll() {
    const selectAll = document.getElementById('uploaded-file-select-all');
    const checkboxes = document.querySelectorAll('#uploaded-files-list .uploaded-file-checkbox');
    
    checkboxes.forEach(cb => {
        cb.checked = selectAll.checked;
    });
}

/**
 * 更新已上传文件全选框状态
 */
function updateUploadedFileSelectAllState() {
    const selectAll = document.getElementById('uploaded-file-select-all');
    const checkboxes = document.querySelectorAll('#uploaded-files-list .uploaded-file-checkbox');
    const checkedCount = document.querySelectorAll('#uploaded-files-list .uploaded-file-checkbox:checked').length;
    
    if (checkboxes.length === 0) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
    } else {
        selectAll.checked = checkedCount === checkboxes.length;
        selectAll.indeterminate = checkedCount > 0 && checkedCount < checkboxes.length;
    }
}

async function reprocessFile(fileId, enterpriseId) {
    try {
        showToast('正在重新处理文件...', 'info');
        
        let url = `${API_BASE}/enterprise/files/${fileId}/process`;
        if (enterpriseId) {
            url += `?enterprise_id=${encodeURIComponent(enterpriseId)}`;
        }
        
        const res = await fetchWithTimeout(url, {
            method: 'POST'
        });
        
        const data = await res.json();
        
        if (data.success) {
            showToast(data.message || '文件处理完成', 'success');
            setTimeout(() => loadUploadedFiles(), 2000);
        } else {
            showToast(data.message || '处理失败', 'error');
        }
    } catch (e) {
        showToast('重新处理失败: ' + e.message, 'error');
    }
}

/**
 * 批量处理所有待处理的文件
 */
async function processAllPendingFiles() {
    if (!confirm('确定要处理所有待处理文件吗？\n\n这将重新处理“待处理”和“待OCR”文件，并尝试再次提取知识内容。')) {
        return;
    }
    
    try {
        showToast('正在批量处理文件...', 'info');
        
        const res = await fetchWithTimeout(`${API_BASE}/enterprise/files/process-all`, {
            method: 'POST'
        });
        
        const data = await res.json();
        
        if (data.success) {
            showToast(data.message || `已处理 ${data.processed_count} 个文件`, 'success');
            
            // 刷新文件列表和知识库
            await loadUploadedFiles();
            if (window.KnowledgeApp) {
                await window.KnowledgeApp.loadKnowledge();
                await window.KnowledgeApp.loadStatistics();
            }
        } else {
            showToast(data.message || '处理失败', 'error');
        }
    } catch (e) {
        console.error("批量处理失败", e);
        showToast('批量处理失败: ' + e.message, 'error');
    }
}

function ensurePendingOcrStatusOption(selectEl) {
    if (!selectEl) return;
    const hasOption = Array.from(selectEl.options || []).some(opt => String(opt.value || '').trim() === 'pending_ocr');
    if (hasOption) return;
    const option = document.createElement('option');
    option.value = 'pending_ocr';
    option.textContent = '待OCR';
    selectEl.appendChild(option);
}

/**
 * 批量删除已上传文件
 */
async function batchDeleteUploadedFiles() {
    const checkboxes = document.querySelectorAll('#uploaded-files-list .uploaded-file-checkbox:checked');
    const fileIds = Array.from(checkboxes).map(cb => cb.value);
    
    if (fileIds.length === 0) {
        showToast('请先选择要删除的文件', 'warning');
        return;
    }
    
    const confirmMsg = `⚠️ 警告：删除操作不可恢复！\n\n确定要删除选中的 ${fileIds.length} 个文件吗？相关的向量数据和知识点也会被删除。`;
    
    if (!confirm(confirmMsg)) {
        return;
    }
    
    try {
        const res = await fetchWithTimeout(`${API_BASE}/rag/files/batch-delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_ids: fileIds })
        });
        
        const data = await res.json();
        
        if (data.success) {
            showToast(data.message || `成功删除 ${data.success_count} 个文件`, 'success');
            document.getElementById('uploaded-file-select-all').checked = false;
            loadUploadedFiles();
            if (window.KnowledgeApp) {
                window.KnowledgeApp.loadKnowledge();
            }
        } else {
            showToast(data.message || '批量删除失败', 'error');
        }
    } catch (e) {
        showToast('批量删除失败: ' + e.message, 'error');
    }
}

/**
 * 显示批量上传模态框
 */
function showUploadModal() {
    const modal = new bootstrap.Modal(document.getElementById('uploadModal'));
    modal.show();
}

/**
 * 开始批量上传
 */
async function startBatchUpload() {
    const files = document.getElementById('batch-file-input').files;
    if (files.length === 0) {
        alert('请选择文件');
        return;
    }
    
    const enterpriseId = 'default';
    
    const list = document.getElementById('batch-upload-list');
    list.innerHTML = '';
    
    for (const file of files) {
        const item = document.createElement('div');
        item.className = 'list-group-item';
        item.innerHTML = `
            <div class="d-flex justify-content-between">
                <span>${escapeHtml(file.name)}</span>
                <span class="batch-status text-muted">等待中...</span>
            </div>
        `;
        list.appendChild(item);
    }
    
    // 逐个上传
    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const item = list.children[i];
        
        try {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('enterprise_id', enterpriseId);
            
            item.querySelector('.batch-status').textContent = '上传中...';
            
            const res = await fetchWithTimeout(`${API_BASE}/rag/upload`, {
                method: 'POST',
                body: formData
            });
            
            if (res.ok) {
                item.querySelector('.batch-status').textContent = '完成';
                item.querySelector('.batch-status').className = 'batch-status text-success';
            } else {
                item.querySelector('.batch-status').textContent = '失败';
                item.querySelector('.batch-status').className = 'batch-status text-danger';
            }
        } catch (e) {
            item.querySelector('.batch-status').textContent = '失败';
            item.querySelector('.batch-status').className = 'batch-status text-danger';
        }
    }
    
    loadUploadedFiles();
}

// ========== 知识图谱功能 ==========

let graphModal;

/**
 * 显示知识图谱
 */
function showGraphRAG() {
    const modalEl = document.getElementById('graphModal');
    if (!modalEl) return;
    graphModal = new bootstrap.Modal(modalEl);
    graphModal.show();
    loadGraphStats();
    modalEl.addEventListener('shown.bs.modal', function onShown() {
        modalEl.removeEventListener('shown.bs.modal', onShown);
        refreshGraph();
    });
}

/**
 * 加载图谱统计
 */
async function loadGraphStats() {
    try {
        const res = await fetchWithTimeout(`${API_BASE}/graph/stats`);
        const stats = await res.json();
        
        const entityCountEl = document.getElementById('graph-entity-count');
        const relationCountEl = document.getElementById('graph-relation-count');
        
        if (entityCountEl) entityCountEl.textContent = stats.entity_count || 0;
        if (relationCountEl) relationCountEl.textContent = stats.relation_count || 0;
        
    } catch (e) {
        console.error("Load graph stats failed", e);
    }
}

/**
 * 刷新图谱
 */
async function refreshGraph() {
    const domainFilter = document.getElementById('graph-domain-filter')?.value || '';
    const typeFilter = document.getElementById('graph-type-filter')?.value || '';
    const searchEl = document.getElementById('graph-search');
    const searchQuery = searchEl ? searchEl.value : '';
    const loadingEl = document.getElementById('graph-loading');
    
    try {
        if (loadingEl) loadingEl.style.display = 'block';
        
        let url = `${API_BASE}/graph/data?query=${encodeURIComponent(searchQuery)}`;
        if (domainFilter) url += `&domain=${encodeURIComponent(domainFilter)}`;
        if (typeFilter) url += `&entity_type=${encodeURIComponent(typeFilter)}`;
        
        const res = await fetchWithTimeout(url);
        const result = await res.json();
        
        if (loadingEl) loadingEl.style.display = 'none';
        renderGraph(result);
        
    } catch (e) {
        if (loadingEl) loadingEl.style.display = 'none';
        showToast('加载图谱失败: ' + e.message, 'error');
    }
}

/**
 * 清空图谱
 */
async function clearGraph() {
    if (!confirm('确定要清空知识图谱吗？此操作不可恢复。')) return;
    
    try {
        const res = await fetchWithTimeout(`${API_BASE}/graph/clear`, { method: 'POST' });
        const result = await res.json();
        
        if (result.success) {
            showToast('图谱已清空', 'success');
            loadGraphStats();
            refreshGraph();
        } else {
            showToast('清空失败: ' + (result.message || '未知错误'), 'error');
        }
        
    } catch (e) {
        showToast('清空失败: ' + e.message, 'error');
    }
}

/**
 * 渲染图谱 (增强版)
 */
function renderGraph(data) {
    const container = document.getElementById('graph-container');
    const canvas = document.getElementById('graph-canvas');
    
    if (!container || !canvas) return;
    
    const ctx = canvas.getContext('2d');
    
    const dpr = window.devicePixelRatio || 1;
    const displayWidth = container.offsetWidth;
    const displayHeight = container.offsetHeight;
    
    if (displayWidth === 0 || displayHeight === 0) {
        setTimeout(() => renderGraph(data), 100);
        return;
    }
    
    canvas.width = displayWidth * dpr;
    canvas.height = displayHeight * dpr;
    canvas.style.width = displayWidth + 'px';
    canvas.style.height = displayHeight + 'px';
    ctx.scale(dpr, dpr);
    
    ctx.clearRect(0, 0, displayWidth, displayHeight);
    
    if (!data.entities || data.entities.length === 0) {
        ctx.fillStyle = '#999';
        ctx.font = '16px Arial';
        ctx.textAlign = 'center';
        ctx.fillText('暂无图谱数据，请点击"从文档提取"按钮生成图谱', displayWidth / 2, displayHeight / 2);
        return;
    }
    
    const colors = {
        'domain': '#FF6B35', 'topic': '#4ECDC4', 'knowledge_point': '#45B7D1',
        'product': '#4CAF50', 'product_module': '#66BB6A', 'feature': '#81C784',
        'service': '#2196F3', 'company': '#FF9800', 'concept': '#9C27B0',
        'faq': '#F44336', 'person': '#00BCD4', 'location': '#795548',
        'event': '#E91E63', 'policy': '#607D8B', 'solution': '#8BC34A',
        'price_plan': '#FFD93D', 'price': '#FFD93D', 'promotion': '#FF7043',
        'technology': '#7C4DFF', 'platform': '#00BFA5', 'process': '#5C6BC0',
        'cooperation': '#26A69A', 'default': '#9E9E9E'
    };
    const domainColors = {
        'attractions': '#FF6B35', 'food': '#4CAF50',
        'transport': '#2196F3', 'accommodation': '#FF9800',
        'itinerary': '#9C27B0', 'price': '#FFD93D',
        'tips': '#26A69A', 'chongqing_travel': '#E91E63'
    };
    const nodeRadiusMap = { 'domain': 40, 'topic': 30 };
    
    const centerX = displayWidth / 2;
    const centerY = displayHeight / 2;
    const radius = Math.min(displayWidth, displayHeight) / 3;
    
    const nodes = data.entities.map((entity, i) => {
        const angle = (2 * Math.PI * i) / data.entities.length;
        const x = centerX + radius * Math.cos(angle);
        const y = centerY + radius * Math.sin(angle);
        const entityType = entity.entityType || entity.type || 'default';
        const domainId = entity.properties?.domain_id || '';
        let color = colors[entityType] || colors['default'];
        if (domainId && domainColors[domainId]) {
            color = domainColors[domainId];
        }
        const r = nodeRadiusMap[entityType] || 25;
        
        return {
            id: entity.id,
            name: entity.name,
            type: entityType,
            description: entity.description || '',
            properties: entity.properties || {},
            x: x, y: y,
            color: color,
            radius: r
        };
    });
    
    const nodeMap = {};
    nodes.forEach(n => nodeMap[n.id] = n);
    
    if (data.relations && data.relations.length > 0) {
        ctx.strokeStyle = '#bbb';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([]);
        
        data.relations.forEach(rel => {
            const source = nodeMap[rel.source_id];
            const target = nodeMap[rel.target_id];
            
            if (source && target) {
                ctx.beginPath();
                ctx.moveTo(source.x, source.y);
                ctx.lineTo(target.x, target.y);
                ctx.stroke();
                
                const midX = (source.x + target.x) / 2;
                const midY = (source.y + target.y) / 2;
                
                ctx.fillStyle = '#666';
                ctx.font = '9px Arial';
                ctx.textAlign = 'center';
                const relType = rel.type || 'related_to';
                ctx.fillText(relType.substring(0, 8), midX, midY);
            }
        });
    }
    
    nodes.forEach(node => {
        const r = node.radius || 25;
        ctx.beginPath();
        ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
        ctx.fillStyle = node.color;
        ctx.fill();
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
        ctx.stroke();
        
        ctx.fillStyle = '#fff';
        ctx.font = r > 30 ? 'bold 13px Arial' : 'bold 11px Arial';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        
        const displayName = node.name.length > 4 ? node.name.substring(0, 4) + '..' : node.name;
        ctx.fillText(displayName, node.x, node.y);
    });
    
    const legendY = 20;
    let legendX = 20;
    ctx.font = '10px Arial';
    ctx.textAlign = 'left';
    
    const typeLabels = {
        'domain': '领域', 'topic': '主题', 'knowledge_point': '知识点',
        'product': '产品', 'service': '服务', 'company': '公司',
        'concept': '概念', 'faq': 'FAQ', 'person': '人物',
        'location': '地点', 'event': '事件', 'policy': '政策',
        'default': '其他'
    };
    
    Object.entries(colors).slice(0, 7).forEach(([type, color]) => {
        ctx.beginPath();
        ctx.arc(legendX, legendY, 6, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.fill();
        
        ctx.fillStyle = '#333';
        ctx.fillText(typeLabels[type] || type, legendX + 10, legendY + 3);
        legendX += 60;
    });
    
    canvas.onclick = function(event) {
        const rect = canvas.getBoundingClientRect();
        const scaleX = displayWidth / rect.width;
        const scaleY = displayHeight / rect.height;
        const x = (event.clientX - rect.left) * scaleX;
        const y = (event.clientY - rect.top) * scaleY;
        
        for (const node of nodes) {
            const dx = x - node.x;
            const dy = y - node.y;
            const r = node.radius || 25;
            if (dx * dx + dy * dy < r * r) {
                showEntityDetail(node, data.relations, nodes);
                break;
            }
        }
    };
}

/**
 * 显示实体详情
 */
function showEntityDetail(node, relations, allNodes) {
    const detailEl = document.getElementById('entity-detail');
    if (!detailEl) return;
    
    const relatedRelations = relations ? relations.filter(r => 
        r.source_id === node.id || r.target_id === node.id
    ) : [];
    
    const typeLabels = {
        'domain': '领域', 'topic': '主题', 'knowledge_point': '知识点',
        'product': '产品', 'service': '服务', 'company': '公司',
        'concept': '概念', 'faq': 'FAQ', 'person': '人物',
        'location': '地点', 'event': '事件', 'policy': '政策',
        'default': '其他'
    };
    
    let html = `
        <div class="row">
            <div class="col-md-6">
                <strong>${escapeHtml(node.name)}</strong>
                <br><small class="text-muted">类型: ${escapeHtml(typeLabels[node.type] || node.type)}</small>
                <br><small>${escapeHtml(node.description || '暂无描述')}</small>
            </div>
            <div class="col-md-6">
                <small><strong>相关关系 (${relatedRelations.length})</strong></small>
                <ul class="list-unstyled mb-0">
    `;
    
    relatedRelations.slice(0, 5).forEach(rel => {
        const isSource = rel.source_id === node.id;
        const otherId = isSource ? rel.target_id : rel.source_id;
        const otherNode = allNodes.find(n => n.id === otherId);
        if (otherNode) {
            const arrow = isSource ? '→' : '←';
            html += `<li><small>${arrow} ${escapeHtml(rel.type || '相关')}: ${escapeHtml(otherNode.name)}</small></li>`;
        }
    });
    
    if (relatedRelations.length > 5) {
        html += `<li><small class="text-muted">...还有 ${relatedRelations.length - 5} 个关系</small></li>`;
    }
    
    html += `</ul></div></div>`;
    
    detailEl.innerHTML = html;
}

/**
 * 从文档提取知识图谱
 */
async function extractFromDocuments() {
    if (!confirm('将从所有已上传文档中提取实体和关系，这可能需要一些时间。是否继续？')) return;
    
    const loadingEl = document.getElementById('graph-loading');
    
    try {
        if (loadingEl) loadingEl.style.display = 'block';
        showToast('正在提取知识图谱...', 'info');
        
        const domainFilter = document.getElementById('graph-domain-filter')?.value || '';
        let url = `${API_BASE}/graph/extract`;
        if (domainFilter) url += `?domain=${domainFilter}`;
        const res = await fetchWithTimeout(url, { method: 'POST' });
        const result = await res.json();
        
        if (loadingEl) loadingEl.style.display = 'none';
        
        if (result.success) {
            showToast(`提取完成: ${result.entities} 个实体, ${result.relations} 个关系`, 'success');
            loadGraphStats();
            refreshGraph();
        } else {
            showToast('提取失败: ' + (result.message || '未知错误'), 'error');
        }
        
    } catch (e) {
        if (loadingEl) loadingEl.style.display = 'none';
        showToast('提取失败: ' + e.message, 'error');
    }
}

// ========== 工具函数 ==========

/**
 * 格式化文件大小
 */
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// ========== 自动回复日志功能 ==========

/**
 * 更新自动回复状态显示
 */
function updateMonitorStatus(isRunning, unreadCount = 0) {
    const startBtn = document.getElementById('btn-start-monitor');
    const stopBtn = document.getElementById('btn-stop-monitor');
    const unreadBadge = document.getElementById('monitor-unread-count');

    if (startBtn) {
        startBtn.classList.toggle('d-none', isRunning);
    }

    if (stopBtn) {
        stopBtn.classList.toggle('d-none', !isRunning);
    }

    if (unreadBadge) {
        unreadBadge.textContent = unreadCount;
        unreadBadge.className = unreadCount > 0 ? 'badge bg-danger' : 'badge bg-secondary';
    }
}

// ========== 初始化 ==========

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', () => {
    updateLicenseBadge(currentLicenseStatus);
    refreshLicenseStatus();
    refreshRemoteControlStatus();
    initSearchForm();
    initSendForm();
    initInteractForm();
    initFileUpload();

    const customerCommentTimeInput = document.getElementById('filter-comment-time');
    if (customerCommentTimeInput && typeof flatpickr !== 'undefined') {
        const customerCommentTimePicker = flatpickr(customerCommentTimeInput, {
            mode: 'range',
            maxDate: 'today',
            locale: 'zh',
            dateFormat: 'Y-m-d',
            altInput: true,
            altFormat: 'Y-m-d',
            allowInput: true,
            onClose: () => loadCustomers(1),
        });
        if (customerCommentTimePicker?.altInput) {
            customerCommentTimePicker.altInput.placeholder = '全部评论时间';
        }
    }

    updateMonitorStatus(false, 0);

    if (isPanelActive('#customer-panel')) {
        loadCustomers(1);
    }

    document.getElementById('filter-comment-time')?.addEventListener('change', () => loadCustomers(1));
    document.getElementById('filter-comment-time')?.addEventListener('blur', () => loadCustomers(1));
    document.getElementById('filter-platform')?.addEventListener('change', () => loadCustomers(1));
    document.getElementById('filter-status')?.addEventListener('change', () => loadCustomers(1));
    document.getElementById('filter-interact-status')?.addEventListener('change', () => loadCustomers(1));
    document.getElementById('customer-page-size')?.addEventListener('change', () => loadCustomers(1));
    document.getElementById('filter-current-task-only')?.addEventListener('change', () => loadCustomers(1));
    document.getElementById('customer-search')?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            loadCustomers(1);
        }
    });
    document.getElementById('customer-search')?.addEventListener('blur', () => loadCustomers(1));
    document.getElementById('license-login-username')?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            submitLicenseLogin();
        }
    });
    document.getElementById('license-login-password')?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            submitLicenseLogin();
        }
    });
    document.getElementById('customer-prev-page')?.addEventListener('click', () => {
        if (customerPageState.page > 1) {
            loadCustomers(customerPageState.page - 1);
        }
    });
    document.getElementById('customer-next-page')?.addEventListener('click', () => {
        if (customerPageState.page < customerPageState.totalPages) {
            loadCustomers(customerPageState.page + 1);
        }
    });
    connectWebSocket();

    document.querySelectorAll('.modal').forEach(modalEl => {
        modalEl.addEventListener('hide.bs.modal', function() {
            const focusedEl = modalEl.querySelector(':focus');
            if (focusedEl) focusedEl.blur();
        });
    });

    let analyticsLoading = false;
    const analyticsRefreshTimer = setInterval(() => {
        if (!pageIsUnloading && document.visibilityState === 'visible' && isPanelActive('#analytics-panel') && !analyticsLoading) {
            analyticsLoading = true;
            loadEnhancedAnalytics().finally(() => { analyticsLoading = false; });
        }
    }, 60000);

    window.addEventListener('beforeunload', () => {
        pageIsUnloading = true;
        if (statusPollTimer) {
            clearTimeout(statusPollTimer);
            statusPollTimer = null;
        }
        clearInterval(analyticsRefreshTimer);
        if (websocket) {
            websocket.onclose = null;
            websocket.close();
            websocket = null;
        }
        if (websocketReconnectTimer) {
            clearTimeout(websocketReconnectTimer);
            websocketReconnectTimer = null;
        }
    });
});
