/**
 * HuoKe UI 辅助模块
 *
 * 抽离 main.js 中的 UI 辅助组件与状态机：Toast、按钮 loading、模态框可访问性、
 * 授权徽章、远控徽章、监听徽章等。挂载到 window.HKUI 命名空间。
 *
 * 与 main.js 的关系：
 *   - 本模块依赖 HKUtils.escapeHtml；
 *   - main.js 仍保留同名函数定义，行为等价（不破坏现有调用）；
 *   - 新代码可直接使用 HKUI.showToast 等接口。
 */
(function (global) {
    'use strict';

    const HKUtils = (global && global.HKUtils) || {};
    const escapeHtml = HKUtils.escapeHtml || function (text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    };

    // ============== Toast 提示 ==============

    /**
     * 创建 Toast 容器 DOM
     */
    function createToastContainer() {
        const container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = 'position: fixed; top: 20px; right: 20px; z-index: 9999; max-width: 400px;';
        document.body.appendChild(container);
        return container;
    }

    /**
     * 显示 Toast 提示
     * @param {string} message
     * @param {'success'|'error'|'warning'|'info'} type
     */
    function showToast(message, type) {
        type = type || 'info';
        const container = document.getElementById('toast-container') || createToastContainer();

        const icons = {
            success: '✅',
            error: '❌',
            warning: '⚠️',
            info: 'ℹ️',
        };

        const colors = {
            success: 'bg-success',
            error: 'bg-danger',
            warning: 'bg-warning text-dark',
            info: 'bg-info',
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
        setTimeout(() => closeToast(toastId), 3000);
    }

    /**
     * 关闭指定 Toast
     */
    function closeToast(toastId) {
        const toast = document.getElementById(toastId);
        if (toast) {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }
    }

    // ============== 按钮 Loading 状态 ==============

    /**
     * 切换按钮的 loading 状态
     * @param {HTMLElement} btn
     * @param {boolean} loading
     * @param {string} loadingText
     */
    function setButtonLoading(btn, loading, loadingText) {
        loadingText = loadingText || '处理中...';
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

    // ============== 模态框可访问性 ==============

    /**
     * 为 Bootstrap 模态框绑定可访问性行为：失焦/恢复焦点/可选隐藏后移除
     */
    function bindModalAccessibility(modalElement, options) {
        options = options || {};
        const removeOnHidden = options.removeOnHidden !== false ? true : Boolean(options.removeOnHidden);
        const restoreFocus = options.restoreFocus !== false;
        if (!modalElement || modalElement.dataset.accessibilityBound === 'true') {
            return modalElement;
        }
        const previousActiveElement = document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null;
        modalElement.dataset.accessibilityBound = 'true';

        modalElement.addEventListener('hide.bs.modal', function () {
            const activeElement = document.activeElement;
            if (activeElement instanceof HTMLElement && modalElement.contains(activeElement)) {
                activeElement.blur();
            }
        });

        modalElement.addEventListener('hidden.bs.modal', function () {
            if (restoreFocus && previousActiveElement && document.contains(previousActiveElement)) {
                try {
                    previousActiveElement.focus();
                } catch (_e) {
                    if (document.body && typeof document.body.focus === 'function') {
                        document.body.focus();
                    }
                }
            } else if (document.body && typeof document.body.focus === 'function') {
                document.body.focus();
            }
            if (removeOnHidden) {
                modalElement.remove();
            }
        });

        return modalElement;
    }

    // ============== 监听状态徽章 ==============

    /**
     * 切换爬虫监听按钮组 / 未读计数
     */
    function updateMonitorStatus(isRunning, unreadCount) {
        isRunning = Boolean(isRunning);
        unreadCount = Number(unreadCount || 0);
        const startBtn = document.getElementById('btn-start-monitor');
        const stopBtn = document.getElementById('btn-stop-monitor');
        const unreadBadge = document.getElementById('monitor-unread-count');

        if (startBtn) startBtn.classList.toggle('d-none', isRunning);
        if (stopBtn) stopBtn.classList.toggle('d-none', !isRunning);
        if (unreadBadge) {
            unreadBadge.textContent = unreadCount;
            unreadBadge.className = unreadCount > 0 ? 'badge bg-danger' : 'badge bg-secondary';
        }
    }

    // ============== 授权徽章 / 模态框状态 ==============

    function getLicenseTypeLabel(licenseType) {
        if (HKUtils.getLicenseTypeLabel) return HKUtils.getLicenseTypeLabel(licenseType);
        return '授权';
    }

    function formatMachineHashPreview(value) {
        if (HKUtils.formatMachineHashPreview) return HKUtils.formatMachineHashPreview(value);
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

    // ============== 远控徽章 ==============

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

    // ============== 命名空间导出 ==============

    const HKUI = {
        // Toast
        showToast,
        createToastContainer,
        closeToast,
        // 按钮
        setButtonLoading,
        // 模态框
        bindModalAccessibility,
        // 监听
        updateMonitorStatus,
        // 授权 / 远控徽章
        updateLicenseBadge,
        updateLicenseModalStatus,
        updateRemoteControlBadge,
        getLicenseTypeLabel,
        formatMachineHashPreview,
    };

    global.HKUI = HKUI;
})(typeof window !== 'undefined' ? window : globalThis);
