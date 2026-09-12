(function () {
    const API_BASE = '/api';

    const CATEGORY_OPTIONS = [
        ['generic', '通用知识'],
        ['product', '产品介绍'],
        ['service', '服务沟通'],
        ['solution', '解决方案'],
        ['policy', '规则政策'],
        ['process', '流程说明'],
        ['company', '公司介绍'],
        ['course', '课程介绍'],
        ['route', '路线行程'],
        ['price', '价格费用'],
        ['tips', '注意事项'],
        ['faq', '常见问题'],
        ['other', '其他']
    ];

    function getSchemaAwareCategoryOptions() {
        const knowledgeApp = window.KnowledgeApp;
        if (knowledgeApp && typeof knowledgeApp.getKnowledgeCategoryOptions === 'function') {
            const options = knowledgeApp.getKnowledgeCategoryOptions();
            if (Array.isArray(options) && options.length) {
                return options.map(([value, label]) => [String(value || '').trim(), String(label || value || '').trim()]);
            }
        }
        return [...CATEGORY_OPTIONS];
    }

    function setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function escapeText(value) {
        if (typeof escapeHtml === 'function') {
            return escapeHtml(value == null ? '' : String(value));
        }
        const text = value == null ? '' : String(value);
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function truncateText(value, maxLength = 120) {
        const text = value == null ? '' : String(value);
        if (text.length <= maxLength) return text;
        return `${text.slice(0, maxLength)}...`;
    }

    function buildCategoryOptions(selectedCategory) {
        const normalized = (selectedCategory || 'other').trim() || 'other';
        const options = getSchemaAwareCategoryOptions();
        if (!options.some(([value]) => value === normalized)) {
            options.unshift([normalized, normalized]);
        }
        return options.map(([value, label]) => `
            <option value="${escapeText(value)}" ${value === normalized ? 'selected' : ''}>${escapeText(label)}</option>
        `).join('');
    }

    function getPendingTypeMeta(itemType) {
        if (itemType === 'knowledge_review') {
            return { label: '知识库待审', badge: 'bg-primary' };
        }
        return { label: '学习待审', badge: 'bg-warning text-dark' };
    }

    function getHistoryStatusMeta(action, status) {
        const normalizedAction = (action || '').toLowerCase();
        const normalizedStatus = (status || '').toLowerCase();
        if (normalizedAction === 'auto_approved') {
            return { label: '自动通过', badge: 'bg-info text-dark' };
        }
        if (normalizedStatus === 'approved') {
            return { label: '已学习', badge: 'bg-success' };
        }
        if (normalizedStatus === 'rejected') {
            return { label: '已拒绝', badge: 'bg-danger' };
        }
        return { label: '处理中', badge: 'bg-secondary' };
    }

    function formatDateTime(value) {
        if (!value) return '-';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return escapeText(value);
        }
        return date.toLocaleString('zh-CN', { hour12: false });
    }

    const LearningApp = {
        openPendingValidationModal() {
            const modalEl = document.getElementById('pendingValidationModal');
            if (!modalEl || typeof bootstrap === 'undefined' || !bootstrap.Modal) return;
            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();
            this.loadPendingValidation();
        },

        openLearnedHistoryModal() {
            const modalEl = document.getElementById('learnedHistoryModal');
            if (!modalEl || typeof bootstrap === 'undefined' || !bootstrap.Modal) return;
            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();
            this.loadLearnedHistory();
        },

        async onPanelActivated() {
            await this.loadLearningStats();
        },

        async loadLearningStats() {
            try {
                const res = await fetchWithTimeout(`${API_BASE}/learning/stats`);
                const data = await res.json();

                setText('stat-learned-total', data.total_learned || data.patterns_learned || 0);
                setText('stat-pending-validation', data.pending_validation || 0);
                setText('stat-pending-validation-btn', data.pending_validation || 0);
                setText('learned-history-count', data.total_learned || data.patterns_learned || 0);
            } catch (e) {
                console.error('Load learning stats failed', e);
            }
        },

        async enableLearning() {
            try {
                const res = await fetchWithTimeout(`${API_BASE}/learning/enable`, { method: 'POST' });
                const data = await res.json();
                showToast(data.message || '学习系统已启用', 'success');
                this.loadLearningStats();
            } catch (e) {
                showToast('启用失败: ' + e.message, 'error');
            }
        },

        async disableLearning() {
            try {
                const res = await fetchWithTimeout(`${API_BASE}/learning/disable`, { method: 'POST' });
                const data = await res.json();
                showToast(data.message || '学习系统已禁用', 'warning');
                this.loadLearningStats();
            } catch (e) {
                showToast('禁用失败: ' + e.message, 'error');
            }
        },

        async loadPendingValidation() {
            try {
                const res = await fetchWithTimeout(`${API_BASE}/learning/pending?top_k=20`);
                const data = await res.json();
                const list = document.getElementById('pending-validation-list');
                const section = document.getElementById('pending-validation-section');
                if (!list || !section) return;

                if (data.items && data.items.length > 0) {
                    section.classList.remove('hidden');

                    setText('stat-pending-validation-btn', data.total || data.items.length || 0);
                    setText('stat-pending-validation', data.total || data.items.length || 0);
                    list.innerHTML = data.items.map(item => {
                        const itemType = getPendingTypeMeta(item.item_type);
                        const confidence = Number(item.confidence || 0);
                        const confidenceLabel = item.item_type === 'knowledge_review'
                            ? '<span class="badge bg-secondary">待人工审核</span>'
                            : `<span class="badge bg-${confidence > 0.8 ? 'success' : 'warning'}">${(confidence * 100).toFixed(0)}%</span>`;

                        return `
                            <tr>
                                <td style="max-width: 220px;">
                                    <div class="fw-semibold text-truncate" title="${escapeText(item.question || '')}">${escapeText(item.question || '')}</div>
                                    <div class="mt-1 d-flex gap-1 flex-wrap">
                                        <span class="badge ${itemType.badge}">${itemType.label}</span>
                                        <span class="badge bg-light text-dark border">${escapeText(item.source || 'unknown')}</span>
                                    </div>
                                    <div class="small text-muted mt-1">${formatDateTime(item.updated_at || item.created_at)}</div>
                                </td>
                                <td><textarea class="form-control form-control-sm" rows="3" id="answer-${escapeText(item.id)}">${escapeText(item.answer || '')}</textarea></td>
                                <td>
                                    <select class="form-select form-select-sm" id="category-${escapeText(item.id)}">
                                        ${buildCategoryOptions(item.category)}
                                    </select>
                                </td>
                                <td>${confidenceLabel}</td>
                                <td>
                                    <button class="btn btn-success btn-sm" data-item-id="${escapeText(item.id)}" data-item-type="${escapeText(item.item_type || 'pending_validation')}" onclick="approveKnowledge(this.dataset.itemId, this.dataset.itemType)">✓ 通过</button>
                                    <button class="btn btn-danger btn-sm" data-item-id="${escapeText(item.id)}" data-item-type="${escapeText(item.item_type || 'pending_validation')}" onclick="rejectKnowledge(this.dataset.itemId, this.dataset.itemType)">✕ 拒绝</button>
                                </td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    section.classList.remove('hidden');
                    setText('stat-pending-validation-btn', 0);
                    setText('stat-pending-validation', 0);
                    list.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-3">暂无待审核知识</td></tr>';
                }
            } catch (e) {
                showToast('加载待审核列表失败: ' + e.message, 'error');
            }
        },

        async loadLearnedHistory() {
            try {
                const res = await fetchWithTimeout(`${API_BASE}/learning/history?limit=50&status=approved`);
                const data = await res.json();
                const list = document.getElementById('learned-history-list');
                const section = document.getElementById('learned-history-section');
                if (!list || !section) return;

                const items = Array.isArray(data.items) ? data.items : [];
                setText('learned-history-count', data.total || items.length || 0);

                if (items.length > 0) {
                    section.classList.remove('hidden');
                    list.innerHTML = items.map(item => {
                        const statusMeta = getHistoryStatusMeta(item.action, item.status);
                        return `
                            <tr>
                                <td style="max-width: 220px;">
                                    <div class="fw-semibold text-truncate" title="${escapeText(item.question || '')}">${escapeText(item.question || '')}</div>
                                </td>
                                <td style="max-width: 380px;">
                                    <div title="${escapeText(item.answer || '')}">${escapeText(truncateText(item.answer || '', 180))}</div>
                                </td>
                                <td>${escapeText(item.category || 'other')}</td>
                                <td><span class="badge ${statusMeta.badge}">${statusMeta.label}</span></td>
                                <td>${formatDateTime(item.created_at || item.timestamp)}</td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    section.classList.remove('hidden');
                    list.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-3">暂无已学习内容</td></tr>';
                }
            } catch (e) {
                showToast('加载已学习内容失败: ' + e.message, 'error');
            }
        },

        async approveKnowledge(itemId, itemType = 'pending_validation') {
            try {
                const answerEl = document.getElementById(`answer-${itemId}`);
                const categoryEl = document.getElementById(`category-${itemId}`);
                const res = await fetchWithTimeout(`${API_BASE}/learning/approve`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        item_id: itemId,
                        item_type: itemType,
                        approved_answer: answerEl && answerEl.value ? answerEl.value : '',
                        category: categoryEl && categoryEl.value ? categoryEl.value : ''
                    })
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message || '已批准入库', 'success');
                } else {
                    showToast(data.message || data.error || '批准失败', 'error');
                }
                this.loadPendingValidation();
                this.loadLearningStats();
                this.loadLearnedHistory();
                if (typeof loadKnowledge === 'function') loadKnowledge();
            } catch (e) {
                showToast('批准失败: ' + e.message, 'error');
            }
        },

        async rejectKnowledge(itemId, itemType = 'pending_validation') {
            try {
                const res = await fetchWithTimeout(`${API_BASE}/learning/reject`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        item_id: itemId,
                        item_type: itemType,
                        reason: ''
                    })
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message || '已拒绝', 'warning');
                    this.loadPendingValidation();
                    this.loadLearningStats();
                    this.loadLearnedHistory();
                    if (itemType === 'knowledge_review' && typeof loadKnowledge === 'function') loadKnowledge();
                } else {
                    showToast(data.message || data.error || '拒绝失败', 'error');
                }
            } catch (e) {
                showToast('拒绝失败: ' + e.message, 'error');
            }
        }
    };

    window.LearningApp = LearningApp;
    window.loadLearningStats = (...args) => LearningApp.loadLearningStats(...args);
    window.enableLearning = (...args) => LearningApp.enableLearning(...args);
    window.disableLearning = (...args) => LearningApp.disableLearning(...args);
    window.openPendingValidationModal = (...args) => LearningApp.openPendingValidationModal(...args);
    window.loadPendingValidation = (...args) => LearningApp.loadPendingValidation(...args);
    window.openLearnedHistoryModal = (...args) => LearningApp.openLearnedHistoryModal(...args);
    window.loadLearnedHistory = (...args) => LearningApp.loadLearnedHistory(...args);
    window.approveKnowledge = (...args) => LearningApp.approveKnowledge(...args);
    window.rejectKnowledge = (...args) => LearningApp.rejectKnowledge(...args);
})();
