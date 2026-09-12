/**
 * HuoKe API/网络层模块
 *
 * 抽离 main.js 中的网络层与轮询退避逻辑：
 *   - fetchWithTimeout：带超时与 AbortController 的 fetch
 *   - fetchJson：fetch + JSON 解析的统一封装
 *   - getRequestErrorMessage：根据后端 payload 推断用户可见错误
 *   - pollBackoff：指数退避轮询调度器
 *
 * 挂载到 window.HKApi 命名空间。
 *
 * 与 main.js 的关系：
 *   - getRequestErrorMessage 依赖 currentLicenseStatus / currentRemoteControlStatus 状态
 *     以及 updateLicenseBadge / updateRemoteControlBadge 徽章更新函数。允许通过
 *     HKApi.setContext({ getLicenseStatus, setLicenseStatus, updateLicenseBadge, ... })
 *     注入依赖。默认从 window 全局读取。
 *   - main.js 仍保留同名函数定义，行为等价（不破坏现有调用）。
 */
(function (global) {
    'use strict';

    const API_BASE_DEFAULT = (typeof global !== 'undefined' && global.API_BASE) || '/api';

    // 注入的依赖上下文（默认从 window 读取）
    let ctx = {
        apiBase: API_BASE_DEFAULT,
        // 授权状态读写
        getLicenseStatus: function () { return global.currentLicenseStatus; },
        setLicenseStatus: function (v) { global.currentLicenseStatus = v; },
        // 远控状态读写
        getRemoteControlStatus: function () { return global.currentRemoteControlStatus; },
        setRemoteControlStatus: function (v) { global.currentRemoteControlStatus = v; },
        // 徽章更新器
        updateLicenseBadge: function (state) {
            if (global.HKUI && typeof global.HKUI.updateLicenseBadge === 'function') {
                global.HKUI.updateLicenseBadge(state);
            } else if (typeof global.updateLicenseBadge === 'function') {
                global.updateLicenseBadge(state);
            }
        },
        updateLicenseModalStatus: function (state) {
            if (global.HKUI && typeof global.HKUI.updateLicenseModalStatus === 'function') {
                global.HKUI.updateLicenseModalStatus(state);
            } else if (typeof global.updateLicenseModalStatus === 'function') {
                global.updateLicenseModalStatus(state);
            }
        },
        updateRemoteControlBadge: function (state) {
            if (global.HKUI && typeof global.HKUI.updateRemoteControlBadge === 'function') {
                global.HKUI.updateRemoteControlBadge(state);
            } else if (typeof global.updateRemoteControlBadge === 'function') {
                global.updateRemoteControlBadge(state);
            }
        },
    };

    /**
     * 注入网络层依赖（apiBase、状态读写、徽章更新器）
     */
    function setContext(overrides) {
        Object.assign(ctx, overrides || {});
    }

    /**
     * 获取 API 基础路径（优先使用注入的 ctx.apiBase，其次 window.API_BASE，最后默认 /api）
     * @returns {string}
     */
    function getApiBase() {
        if (ctx.apiBase) return ctx.apiBase;
        if (global.API_BASE) return global.API_BASE;
        return API_BASE_DEFAULT;
    }

    // ============== 轮询退避 ==============
    /**
     * 指数退避轮询调度器
     *  - baseInterval：基础间隔（毫秒）
     *  - maxInterval：最大间隔（毫秒）
     *  - currentInterval：当前间隔，连续失败时按 2 的幂增长，直到 maxInterval
     *  - consecutiveErrors：连续错误次数
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
        },

        reset() {
            this.consecutiveErrors = 0;
            this.currentInterval = this.baseInterval;
        },
    };

    // ============== fetch 封装 ==============

    /**
     * 带超时的 fetch，自动解析错误 payload
     * @param {string} url
     * @param {object} options
     * @param {number} timeout 超时时间(毫秒)，默认 15 秒
     */
    async function fetchWithTimeout(url, options, timeout) {
        options = options || {};
        timeout = (typeof timeout === 'number' && timeout > 0) ? timeout : 15000;

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), timeout);
        try {
            const response = await fetch(url, Object.assign({}, options, { signal: controller.signal }));
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

                if (errorPayload && errorPayload.message) {
                    errorMessage = errorPayload.message;
                } else if (errorPayload && errorPayload.detail) {
                    errorMessage = errorPayload.detail;
                }

                const error = new Error(errorMessage);
                error.status = response.status;
                error.responseData = errorPayload;
                throw error;
            }
            return response;
        } catch (e) {
            if (e && e.name === 'AbortError') {
                throw new DOMException(`Request timeout after ${timeout}ms`, 'AbortError');
            }
            throw e;
        } finally {
            clearTimeout(timeoutId);
        }
    }

    /**
     * fetch + 自动 JSON 解析的统一封装
     * @param {string} url
     * @param {object} options
     * @param {number} timeout
     * @returns {Promise<{ok: boolean, status: number, data: any, error: Error|null}>}
     */
    async function fetchJson(url, options, timeout) {
        try {
            const res = await fetchWithTimeout(url, options || {}, timeout);
            let data = null;
            try {
                data = await res.json();
            } catch (_e) {
                data = null;
            }
            return { ok: true, status: res.status, data, error: null };
        } catch (error) {
            return {
                ok: false,
                status: (error && error.status) || 0,
                data: (error && error.responseData) || null,
                error,
            };
        }
    }

    // ============== 错误信息解析 ==============

    /**
     * 根据错误对象提取用户可读的错误信息，同时处理授权/远控状态回写
     * @param {Error} error
     * @param {string} fallback
     */
    function getRequestErrorMessage(error, fallback) {
        fallback = fallback || '请求失败';
        const responseData = (error && error.responseData) || {};
        const licenseStatus = responseData.license_status || {};
        const remoteControlStatus = responseData.remote_control_status || {};

        if (licenseStatus && Object.keys(licenseStatus).length > 0) {
            ctx.setLicenseStatus(licenseStatus);
            ctx.updateLicenseBadge(licenseStatus);
            ctx.updateLicenseModalStatus(licenseStatus);
        }

        if (licenseStatus.status === 'expired') {
            return '授权已到期，请和供应商联系获取使用权限';
        }
        if (licenseStatus.status === 'clock_rollback') {
            return '检测到系统时间被回拨，请校准电脑时间后重试，或联系供应商获取使用权限';
        }
        if (licenseStatus.status === 'missing') {
            return '软件尚未激活，请先激活后再使用该功能';
        }
        if (licenseStatus.status === 'invalid' || licenseStatus.status === 'machine_mismatch') {
            return '授权无效，请使用新版激活码重新激活，或联系供应商获取使用权限';
        }

        if (responseData.message) {
            if (remoteControlStatus && Object.keys(remoteControlStatus).length > 0) {
                ctx.setRemoteControlStatus(remoteControlStatus);
                ctx.updateRemoteControlBadge(remoteControlStatus);
            }
            return responseData.message;
        }

        if (error && error.message) {
            return error.message;
        }
        return fallback;
    }

    // ============== 命名空间导出 ==============

    const HKApi = {
        setContext,
        getApiBase,
        pollBackoff,
        fetchWithTimeout,
        fetchJson,
        getRequestErrorMessage,
    };

    global.HKApi = HKApi;
})(typeof window !== 'undefined' ? window : globalThis);
