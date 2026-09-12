/**
 * 知识库页面交互逻辑
 * 行业知识运营系统
 */

/**
 * 带超时的fetch请求
 * @param {string} url - 请求URL
 * @param {object} options - fetch选项
 * @param {number} timeout - 超时时间(毫秒)，默认15秒
 */
async function knowledgeFetchWithTimeout(url, options = {}, timeout = 15000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);
    try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        if (!response.ok) {
            let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
            try {
                const contentType = String(response.headers.get('content-type') || '').toLowerCase();
                if (contentType.includes('application/json')) {
                    const payload = await response.clone().json();
                    errorMessage = payload.message || payload.detail || payload.reason || errorMessage;
                } else {
                    const text = String(await response.clone().text() || '').trim();
                    if (text) errorMessage = text;
                }
            } catch (_parseError) {
                // 忽略错误体解析失败，保留 HTTP 状态信息。
            }
            throw new Error(errorMessage);
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

const KnowledgeApp = {
    _echartsLoaderPromise: null,

    CATEGORY_LABELS: {
        'generic': '📌 通用知识',
        'product': '📦 产品介绍',
        'service': '💬 服务沟通',
        'solution': '🧩 解决方案',
        'policy': '📋 规则政策',
        'process': '🪜 流程说明',
        'faq': '❓ 常见问题',
        'company': '🏢 公司介绍',
        'price': '💰 价格费用',
        'course': '🎓 课程介绍',
        'attractions': '🏛️ 场景介绍',
        'attraction': '🏛️ 场景介绍',
        'food': '🍜 美食推荐',
        'transport': '🚇 交通出行',
        'accommodation': '🏨 住宿推荐',
        'hotel': '🏨 住宿推荐',
        'itinerary': '📅 行程规划',
        'tips': '📝 注意事项',
        'other': '📁 其他'
    },

    ENTITY_LABELS: {
        route: '线路/路线',
        service: '服务项目',
        product: '产品',
        package: '套餐',
        solution: '解决方案'
    },

    TOPIC_LABELS: {
        overview: '概览',
        price: '价格',
        schedule: '行程',
        service: '服务',
        tips: '提醒',
        attractions: '景点',
        process: '流程',
        comparison: '对比',
        booking: '报名',
        details: '详情'
    },

    DEFAULT_KNOWLEDGE_CATEGORY_OPTIONS: [
        ['generic', '📌 通用知识'],
        ['product', '📦 产品介绍'],
        ['service', '💬 服务沟通'],
        ['solution', '🧩 解决方案'],
        ['policy', '📋 规则政策'],
        ['process', '🪜 流程说明'],
        ['company', '🏢 公司介绍'],
        ['faq', '❓ 常见问题'],
        ['price', '💰 价格费用'],
        ['tips', '📝 注意事项'],
        ['course', '🎓 课程介绍'],
        ['other', '📁 其他']
    ],

    DEFAULT_KNOWLEDGE_DOMAIN_OPTIONS: [
        ['generic', '🌐 通用'],
        ['product', '📦 产品'],
        ['service', '💼 服务'],
        ['solution', '🧩 方案'],
        ['policy', '📘 政策'],
        ['company', '🏢 公司'],
        ['process', '🪜 流程'],
        ['package', '📦 套餐'],
        ['course', '🎓 课程'],
        ['route', '🛣️ 路线/线路'],
        ['price', '💰 价格费用'],
        ['tips', '📝 说明/提醒'],
        ['other', '📁 其他']
    ],

    getCategoryLabel(category) {
        return this.CATEGORY_LABELS[category] || this.escapeHtml(category || '-');
    },

    getIndustryLabel(code) {
        return this.escapeHtml(code || '-');
    },

    getEntityLabel(entityType) {
        return this.ENTITY_LABELS[entityType] || this.escapeHtml(entityType || '-');
    },

    getSchemaLabel(schemaId) {
        const match = (this.state.industrySchemas || []).find(item => item.schema_id === schemaId);
        return this.escapeHtml((match && (match.display_name || match.schema_id)) || schemaId || '未归类');
    },

    getTopicLabel(topic) {
        return this.TOPIC_LABELS[topic] || this.escapeHtml(topic || '未标注');
    },

    getKnowledgeSchemaId(item = {}) {
        return String((item.metadata || {}).schema_id || '').trim();
    },

    renderKnowledgeBadges(item = {}) {
        const parts = [];
        const schemaId = this.getKnowledgeSchemaId(item);
        if (schemaId) {
            parts.push(`<span class="badge bg-primary-subtle text-primary border">${this.getSchemaLabel(schemaId)}</span>`);
        }
        if (item.domain) {
            parts.push(`<span class="badge bg-light text-dark border">${this.getEntityLabel(item.domain)}</span>`);
        }
        if (item.topic) {
            parts.push(`<span class="badge bg-light text-secondary border">${this.getTopicLabel(item.topic)}</span>`);
        }
        return parts.join(' ');
    },

    state: {
        knowledge: [],
        uploadedFiles: [],
        currentPage: 1,
        pageSize: 50,
        totalPages: 1,
        selectedKnowledge: new Set(),
        selectedFiles: new Set(),
        theme: 'light',
        currentWorkspace: ['assets', 'structuring', 'schema'].includes(localStorage.getItem('kb-workspace')) ? localStorage.getItem('kb-workspace') : 'assets',
        initializedWorkspaces: new Set(),
        commonLoaded: false,
        enterprises: [],
        structuringDocuments: [],
        structuringDrafts: [],
        structuringStats: null,
        currentStructuringSubtab: ['detail', 'history', 'review'].includes(localStorage.getItem('kb-structuring-subtab')) ? localStorage.getItem('kb-structuring-subtab') : 'detail',
        structuringHistoryExpanded: {
            drafts: false,
            published: false,
            timeline: false
        },
        selectedStructuringDocumentId: '',
        selectedStructuringDocumentDetail: null,
        selectedStructuringDocumentHistory: null,
        selectedStructuringDraftId: '',
        selectedStructuringDrafts: new Set(),
        industrySchemas: [],
        industrySchemaTemplate: null,
        industrySchemaSettings: null,
        activeIndustrySchemaId: '',
        currentIndustrySchema: null,
        industrySchemaEditorMode: 'form'
    },
    
    init() {
        this.loadTheme();
        this.applyWorkspaceVisibility();
        this.bindIndustrySchemaFormEvents();
        this.bindStructuringWorkspaceEvents();
        this.renderKnowledgeTaxonomyOptions();
        const knowledgePanel = document.getElementById('knowledge-panel');
        if (knowledgePanel && knowledgePanel.classList.contains('active') && knowledgePanel.classList.contains('show')) {
            this.onPanelActivated();
        }
    },

    async onPanelActivated() {
        await this.loadCommonMetadata();
        await this.refreshTopStatistics();
        this.applyWorkspaceVisibility();
        await this.loadWorkspaceData(this.state.currentWorkspace);
    },

    async refreshTopStatistics() {
        await this.loadStatistics();
        if (window.LearningApp && typeof window.LearningApp.loadLearningStats === 'function') {
            await window.LearningApp.loadLearningStats();
        }
    },

    async loadCommonMetadata() {
        if (this.state.commonLoaded) return;
        const tasks = [];
        if (typeof loadEnterprises === 'function') {
            tasks.push(Promise.resolve(loadEnterprises()).catch(err => console.warn('加载企业列表失败:', err)));
        }
        tasks.push(this.loadEnterpriseSuggestions().catch(err => console.warn('加载企业联想失败:', err)));
        if (typeof loadKnowledgeCategories === 'function') {
            tasks.push(Promise.resolve(loadKnowledgeCategories()).catch(err => console.warn('加载知识分类失败:', err)));
        }
        await Promise.all(tasks);
        this.state.commonLoaded = true;
        this.renderStructuringEnterpriseOptions();
    },

    applyWorkspaceVisibility() {
        const workspace = this.state.currentWorkspace || 'assets';
        const knowledgePanel = document.getElementById('knowledge-panel') || document;
        knowledgePanel.querySelectorAll('[data-knowledge-workspace]').forEach(section => {
            const target = section.getAttribute('data-knowledge-workspace');
            section.classList.toggle('hidden', target !== workspace);
        });

        ['assets', 'structuring', 'schema'].forEach(name => {
            const btn = document.getElementById(`knowledge-workspace-${name}`);
            if (!btn) return;
            const active = name === workspace;
            btn.classList.toggle('active', active);
            btn.classList.toggle('btn-primary', active);
            btn.classList.toggle('btn-outline-primary', !active);
        });

        this.applyStructuringSubtabVisibility();
    },

    applyStructuringSubtabVisibility() {
        const current = this.state.currentStructuringSubtab || 'detail';
        const knowledgePanel = document.getElementById('knowledge-panel') || document;
        knowledgePanel.querySelectorAll('[data-structuring-subtab]').forEach(section => {
            const target = section.getAttribute('data-structuring-subtab');
            section.classList.toggle('hidden', target !== current);
        });
        ['detail', 'history', 'review'].forEach(name => {
            const btn = document.getElementById(`doc-structuring-subtab-${name}-btn`);
            if (!btn) return;
            const active = name === current;
            btn.classList.toggle('btn-primary', active);
            btn.classList.toggle('btn-outline-primary', !active);
        });
    },

    switchStructuringSubtab(tab) {
        if (!['detail', 'history', 'review'].includes(tab)) return;
        this.state.currentStructuringSubtab = tab;
        localStorage.setItem('kb-structuring-subtab', tab);
        this.applyStructuringSubtabVisibility();
    },

    toggleStructuringHistoryList(section) {
        if (!['drafts', 'published', 'timeline'].includes(section)) return;
        this.state.structuringHistoryExpanded[section] = !this.state.structuringHistoryExpanded[section];
        this.renderStructuringHistoryPanel(this.state.selectedStructuringDocumentHistory);
    },

    bindIndustrySchemaFormEvents() {
        if (this._industrySchemaFormBound) return;
        this._industrySchemaFormBound = true;

        const staticIds = [
            'industry-schema-id',
            'industry-schema-id-manual-input',
            'industry-schema-display-name',
            'industry-schema-industry-code',
            'industry-schema-entity-type',
            'industry-schema-description',
            'industry-schema-bundle-type',
            'industry-schema-comparison-fields',
            'industry-schema-followup-fields',
            'industry-schema-setting-active',
            'industry-schema-setting-resolution-mode',
            'industry-schema-setting-fallback',
            'industry-schema-setting-bindings'
        ];
        staticIds.forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;
            el.addEventListener('input', () => this.handleIndustrySchemaFormChanged());
            el.addEventListener('change', () => this.handleIndustrySchemaFormChanged());
        });

        ['industry-schema-form-editor', 'industry-schema-fields-editor', 'industry-schema-intents-editor'].forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;
            el.addEventListener('input', () => this.handleIndustrySchemaFormChanged());
            el.addEventListener('change', () => this.handleIndustrySchemaFormChanged());
        });
        const bindingsEditor = document.getElementById('industry-schema-setting-bindings-editor');
        if (bindingsEditor) {
            bindingsEditor.addEventListener('input', () => this.handleIndustrySchemaFormChanged());
            bindingsEditor.addEventListener('change', () => this.handleIndustrySchemaFormChanged());
        }
        ['industry-schema-setting-require-binding', 'industry-schema-setting-allow-override'].forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;
            el.addEventListener('change', () => this.handleIndustrySchemaFormChanged());
        });
        const manualIdToggle = document.getElementById('industry-schema-id-manual');
        if (manualIdToggle) {
            manualIdToggle.addEventListener('change', () => {
                const manualInput = document.getElementById('industry-schema-id-manual-input');
                if (manualInput) {
                    manualInput.disabled = !manualIdToggle.checked;
                    if (!manualIdToggle.checked) {
                        manualInput.value = '';
                    }
                }
                this.handleIndustrySchemaFormChanged();
            });
        }

        this.switchIndustrySchemaEditorMode(this.state.industrySchemaEditorMode || 'form');
    },

    bindStructuringWorkspaceEvents() {
        if (this._structuringWorkspaceBound) return;
        this._structuringWorkspaceBound = true;
        this.bindStructuringUploadInput();
        const enterpriseSelect = document.getElementById('doc-structuring-enterprise');
        if (enterpriseSelect) {
            enterpriseSelect.addEventListener('change', async () => {
                this.state.selectedStructuringDocumentId = '';
                this.state.selectedStructuringDraftId = '';
                this.state.selectedStructuringDrafts.clear();
                await this.reloadStructuringWorkspace();
            });
        }
    },

    getIndustryCodeSuggestions() {
        const templateCode = this.state.industrySchemaTemplate?.industry_code || '';
        const values = [
            templateCode,
            ...(this.state.industrySchemas || []).map(item => item.industry_code || ''),
            this.state.currentIndustrySchema?.industry_code || ''
        ];
        return Array.from(new Set(values.map(value => String(value || '').trim()).filter(Boolean)));
    },

    getEntityTypeSuggestions() {
        const templateEntity = this.state.industrySchemaTemplate?.entity_type || '';
        const values = [
            templateEntity,
            ...(this.state.industrySchemas || []).map(item => item.entity_type || ''),
            this.state.currentIndustrySchema?.entity_type || ''
        ];
        return Array.from(new Set(values.map(value => String(value || '').trim()).filter(Boolean)));
    },

    getIndustrySchemaIdSuggestions() {
        const values = [
            this.state.activeIndustrySchemaId,
            this.state.industrySchemaSettings?.fallback_schema_id || '',
            ...(this.state.industrySchemas || []).map(item => item.schema_id || ''),
            this.state.currentIndustrySchema?.schema_id || ''
        ];
        return Array.from(new Set(values.map(value => String(value || '').trim()).filter(Boolean)));
    },

    getEnterpriseSuggestions() {
        return (this.state.enterprises || []).map(item => String(item.id || '').trim()).filter(Boolean);
    },

    renderIndustrySchemaDatalists() {
        const render = (id, values) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.innerHTML = values.map(value => `<option value="${this.escapeHtml(value)}"></option>`).join('');
        };
        render('industry-schema-industry-code-options', this.getIndustryCodeSuggestions());
        render('industry-schema-entity-type-options', this.getEntityTypeSuggestions());
        render('industry-schema-id-options', this.getIndustrySchemaIdSuggestions());
        render('industry-schema-enterprise-options', this.getEnterpriseSuggestions());
    },

    getCurrentCategoryProfiles() {
        const schema = this.state.currentIndustrySchema || this.state.industrySchemaTemplate || {};
        const metadata = schema.metadata || {};
        return metadata.category_profiles || {};
    },

    getKnowledgeCategoryOptions() {
        const options = new Map(this.DEFAULT_KNOWLEDGE_CATEGORY_OPTIONS);
        Object.entries(this.getCurrentCategoryProfiles()).forEach(([key, profile]) => {
            const normalizedKey = String(key || '').trim();
            if (!normalizedKey) return;
            const label = String((profile || {}).label || normalizedKey).trim();
            if (!options.has(normalizedKey)) {
                options.set(normalizedKey, label);
            }
        });
        return Array.from(options.entries());
    },

    getKnowledgeDomainOptions() {
        const options = new Map(this.DEFAULT_KNOWLEDGE_DOMAIN_OPTIONS);
        const entityType = String(this.state.currentIndustrySchema?.entity_type || '').trim();
        if (entityType && !options.has(entityType)) {
            options.set(entityType, this.getEntityLabel(entityType));
        }
        return Array.from(options.entries());
    },

    renderSelectOptions(elementId, options, selectedValue) {
        const el = document.getElementById(elementId);
        if (!el) return;
        const normalized = String(selectedValue || '').trim();
        const entries = [...options];
        if (normalized && !entries.some(([value]) => value === normalized)) {
            entries.unshift([normalized, normalized]);
        }
        el.innerHTML = entries.map(([value, label]) => (
            `<option value="${this.escapeHtml(value)}" ${value === normalized ? 'selected' : ''}>${this.escapeHtml(label)}</option>`
        )).join('');
    },

    renderKnowledgeTaxonomyOptions(selectedCategory = '', selectedDomain = '') {
        this.renderSelectOptions('knowledge-category', this.getKnowledgeCategoryOptions(), selectedCategory || 'generic');
        this.renderSelectOptions('knowledge-domain', this.getKnowledgeDomainOptions(), selectedDomain || 'generic');
    },

    renderIndustrySchemaSettingsSummary(settings = {}) {
        const setText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        const resolutionLabel = settings.default_resolution_policy === 'fallback_generic'
            ? '自动回到通用模板'
            : settings.default_resolution_policy === 'fail_closed'
                ? '先不自动匹配，避免串模板'
                : '-';
        setText('industry-schema-resolution-mode', resolutionLabel);
        setText('industry-schema-fallback', settings.fallback_schema_id || '-');
        setText('industry-schema-require-binding', settings.require_enterprise_binding ? '是，必须先绑定' : '否，允许按默认规则处理');
        setText('industry-schema-allow-override', settings.allow_preferred_schema_override ? '是，可以临时指定' : '否，固定按配置处理');
    },

    renderIndustrySchemaBindingsEditor(bindings = {}) {
        const container = document.getElementById('industry-schema-setting-bindings-editor');
        if (!container) return;
        const entries = Object.entries(bindings || {});
        if (!entries.length) {
            container.innerHTML = `
                <div class="border rounded p-2 bg-light small text-muted">
                    暂无企业绑定。可按企业 ID 显式绑定到某个 schema，避免继续依赖全局默认值。
                </div>
            `;
            return;
        }
        container.innerHTML = entries.map(([enterpriseId, schemaId], index) => `
            <div class="industry-schema-binding-row border rounded p-2">
                <div class="row g-2 align-items-center">
                    <div class="col-md-5">
                        <label class="form-label small mb-1">企业 ID</label>
                        <input type="text" class="form-control form-control-sm industry-schema-binding-enterprise" list="industry-schema-enterprise-options" value="${this.escapeHtml(enterpriseId)}" placeholder="例如：enterprise-a">
                    </div>
                    <div class="col-md-5">
                        <label class="form-label small mb-1">绑定 Schema</label>
                        <input type="text" class="form-control form-control-sm industry-schema-binding-schema" list="industry-schema-id-options" value="${this.escapeHtml(schemaId)}" placeholder="例如：generic.service_sales">
                    </div>
                    <div class="col-md-2 text-end">
                        <label class="form-label small mb-1 d-block">&nbsp;</label>
                        <button type="button" class="btn btn-sm btn-outline-danger" onclick="KnowledgeApp.removeIndustrySchemaBindingRow(${index})">删除</button>
                    </div>
                </div>
            </div>
        `).join('');
    },

    populateIndustrySchemaSettingsForm(settings = {}) {
        const normalized = settings || {};
        this.state.industrySchemaSettings = normalized;
        this.state.activeIndustrySchemaId = String(normalized.active_schema_id || '').trim();
        this.setIndustrySchemaFormValue('industry-schema-setting-active', this.state.activeIndustrySchemaId || '');
        this.setIndustrySchemaFormValue('industry-schema-setting-resolution-mode', normalized.default_resolution_policy || 'fail_closed');
        this.setIndustrySchemaFormValue('industry-schema-setting-fallback', normalized.fallback_schema_id || '');
        const requireBindingEl = document.getElementById('industry-schema-setting-require-binding');
        if (requireBindingEl) requireBindingEl.checked = Boolean(normalized.require_enterprise_binding);
        const allowOverrideEl = document.getElementById('industry-schema-setting-allow-override');
        if (allowOverrideEl) allowOverrideEl.checked = Boolean(normalized.allow_preferred_schema_override);
        this.renderIndustrySchemaBindingsEditor(normalized.enterprise_schema_bindings || {});
        this.renderIndustrySchemaSettingsSummary(normalized);
        const previewModeEl = document.getElementById('industry-schema-preview-mode');
        if (previewModeEl && !previewModeEl.value) {
            previewModeEl.value = '';
        }
    },

    async loadEnterpriseSuggestions() {
        const response = await knowledgeFetchWithTimeout('/api/enterprises');
        const data = await response.json();
        this.state.enterprises = Array.isArray(data) ? data : [];
        this.renderIndustrySchemaDatalists();
    },

    addIndustrySchemaBindingRow(entry = null) {
        const payload = this.state.industrySchemaSettings || {};
        const bindings = { ...(payload.enterprise_schema_bindings || {}) };
        let nextEnterpriseId = (entry && entry.enterpriseId) || '';
        let nextSchemaId = (entry && entry.schemaId) || '';
        if (!nextEnterpriseId) {
            let index = 1;
            nextEnterpriseId = `enterprise-${index}`;
            while (bindings[nextEnterpriseId]) {
                index += 1;
                nextEnterpriseId = `enterprise-${index}`;
            }
        }
        if (!nextSchemaId) {
            nextSchemaId = this.state.activeIndustrySchemaId || this.state.currentIndustrySchema?.schema_id || '';
        }
        bindings[nextEnterpriseId] = nextSchemaId;
        this.state.industrySchemaSettings = {
            ...(this.state.industrySchemaSettings || {}),
            enterprise_schema_bindings: bindings
        };
        this.renderIndustrySchemaBindingsEditor(bindings);
        this.handleIndustrySchemaFormChanged();
    },

    removeIndustrySchemaBindingRow(index) {
        const bindings = this._collectBindingsFromEditor();
        const entries = Object.entries(bindings).filter((_, idx) => idx !== index);
        const nextBindings = Object.fromEntries(entries);
        this.state.industrySchemaSettings = {
            ...(this.state.industrySchemaSettings || {}),
            enterprise_schema_bindings: nextBindings
        };
        this.renderIndustrySchemaBindingsEditor(nextBindings);
        this.handleIndustrySchemaFormChanged();
    },

    _collectBindingsFromEditor() {
        const rows = Array.from(document.querySelectorAll('#industry-schema-setting-bindings-editor .industry-schema-binding-row'));
        const bindings = {};
        rows.forEach(row => {
            const enterpriseId = String(row.querySelector('.industry-schema-binding-enterprise')?.value || '').trim();
            const schemaId = String(row.querySelector('.industry-schema-binding-schema')?.value || '').trim();
            if (enterpriseId && schemaId) {
                bindings[enterpriseId] = schemaId;
            }
        });
        return bindings;
    },

    collectIndustrySchemaSettingsPayload() {
        const bindings = this._collectBindingsFromEditor();
        const bindingsEl = document.getElementById('industry-schema-setting-bindings');
        if (bindingsEl) {
            bindingsEl.value = JSON.stringify(bindings, null, 2);
        }
        return {
            active_schema_id: (document.getElementById('industry-schema-setting-active')?.value || document.getElementById('industry-schema-id')?.value || '').trim(),
            default_resolution_policy: (document.getElementById('industry-schema-setting-resolution-mode')?.value || 'fail_closed').trim(),
            fallback_schema_id: (document.getElementById('industry-schema-setting-fallback')?.value || '').trim(),
            require_enterprise_binding: document.getElementById('industry-schema-setting-require-binding')?.checked === true,
            allow_preferred_schema_override: document.getElementById('industry-schema-setting-allow-override')?.checked === true,
            enterprise_schema_bindings: bindings
        };
    },

    renderIndustrySchemaPreviewResult(result = null) {
        const el = document.getElementById('industry-schema-preview-result');
        if (!el) return;
        if (!result) {
            el.textContent = '这里会显示该企业最终会使用哪个模板，以及为什么会命中它。';
            return;
        }
        const resolved = this.escapeHtml(result.resolved_schema_id || '未命中');
        const sourceMap = {
            preferred_schema_override: '手动临时指定模板',
            enterprise_profile_binding: '企业资料里已绑定模板',
            settings_binding: '当前页面里配置的企业绑定',
            fallback_schema: '按规则回到通用模板',
            active_schema: '直接使用当前默认模板',
            default_schema: '使用系统默认模板',
            fail_closed_missing_enterprise: '未提供企业，已停止自动匹配',
            fail_closed_missing_binding: '企业未绑定模板，已停止自动匹配',
            unresolved: '没有找到可用模板'
        };
        const modeMap = {
            fail_closed: '严格绑定',
            fallback_generic: '回到通用模板'
        };
        const source = this.escapeHtml(sourceMap[result.source] || result.source || '未知');
        const mode = this.escapeHtml(modeMap[result.effective_resolution_mode] || result.effective_resolution_mode || '-');
        const details = result.details || {};
        const detailParts = [];
        if (details.profile_binding) detailParts.push(`企业资料已绑定：${this.escapeHtml(details.profile_binding)}`);
        if (details.settings_binding) detailParts.push(`页面绑定表命中：${this.escapeHtml(details.settings_binding)}`);
        detailParts.push(`严格绑定：${details.require_enterprise_binding ? '开启' : '关闭'}`);
        detailParts.push(`允许临时指定：${details.allow_preferred_schema_override ? '开启' : '关闭'}`);
        el.innerHTML = `
            <div><strong>最终模板：</strong>${resolved}</div>
            <div><strong>命中原因：</strong>${source}</div>
            <div><strong>解析模式：</strong>${mode}</div>
            <div class="text-muted mt-1">${detailParts.join('；')}</div>
        `;
    },

    async previewIndustrySchemaResolution() {
        try {
            const payload = {
                enterprise_id: (document.getElementById('industry-schema-preview-enterprise')?.value || '').trim(),
                preferred_schema_id: (document.getElementById('industry-schema-preview-preferred')?.value || '').trim(),
                resolution_mode: (document.getElementById('industry-schema-preview-mode')?.value || '').trim()
            };
            const response = await knowledgeFetchWithTimeout('/api/industry-schemas/preview-resolution', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (data.success) {
                this.renderIndustrySchemaPreviewResult(data.result || {});
                return;
            }
            this.renderIndustrySchemaPreviewResult(null);
            this.showToast(data.message || '预览失败', 'error');
        } catch (error) {
            this.renderIndustrySchemaPreviewResult(null);
            this.showToast(`预览失败: ${error.message}`, 'error');
        }
    },

    switchIndustrySchemaEditorMode(mode = 'form') {
        this.state.industrySchemaEditorMode = mode === 'json' ? 'json' : 'form';
        const formPanel = document.getElementById('industry-schema-form-editor');
        const jsonPanel = document.getElementById('industry-schema-json-preview-panel');
        const formBtn = document.getElementById('industry-schema-mode-form');
        const jsonBtn = document.getElementById('industry-schema-mode-json');
        if (formPanel) formPanel.classList.toggle('hidden', this.state.industrySchemaEditorMode !== 'form');
        if (jsonPanel) jsonPanel.classList.toggle('hidden', this.state.industrySchemaEditorMode !== 'json');
        if (formBtn) {
            formBtn.classList.toggle('btn-primary', this.state.industrySchemaEditorMode === 'form');
            formBtn.classList.toggle('btn-outline-primary', this.state.industrySchemaEditorMode !== 'form');
        }
        if (jsonBtn) {
            jsonBtn.classList.toggle('btn-primary', this.state.industrySchemaEditorMode === 'json');
            jsonBtn.classList.toggle('btn-outline-primary', this.state.industrySchemaEditorMode !== 'json');
        }
        this.syncIndustrySchemaJsonPreview();
    },

    async switchWorkspace(workspace, options = {}) {
        if (!workspace) return;
        this.state.currentWorkspace = workspace;
        localStorage.setItem('kb-workspace', workspace);
        this.applyWorkspaceVisibility();

        const knowledgePanel = document.getElementById('knowledge-panel');
        if (knowledgePanel && knowledgePanel.classList.contains('active') && knowledgePanel.classList.contains('show')) {
            await this.loadWorkspaceData(workspace, options);
        }
    },

    async loadWorkspaceData(workspace, options = {}) {
        const forceReload = Boolean(options.forceReload);
        if (!forceReload && this.state.initializedWorkspaces.has(workspace)) {
            return;
        }

        if (workspace === 'assets') {
            await Promise.all([
                this.loadKnowledge(),
                this.loadUploadedFiles()
            ]);
        } else if (workspace === 'structuring') {
            await Promise.all([
                this.loadStructuringStats(),
                this.loadStructuringDocuments()
            ]);
        } else if (workspace === 'schema') {
            await this.loadIndustrySchemas(forceReload);
        }

        this.state.initializedWorkspaces.add(workspace);
    },

    async loadStatistics() {
        const setElementText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };

        try {
            const response = await knowledgeFetchWithTimeout('/api/knowledge/statistics/overview');
            const data = await response.json();
            
            if (data.success || data.statistics) {
                const stats = data.statistics || data.data || {};
                
                setElementText('stat-knowledge-total', stats.total_count || stats.total || 0);
                setElementText('stat-knowledge-enabled', stats.enabled_count || stats.enabled || 0);
                setElementText('stat-pending-validation', stats.pending_validation || stats.pendingValidation || 0);
                setElementText('stat-learned-total', stats.learned_total || stats.learnedTotal || 0);
                setElementText('stat-pending-validation-btn', stats.pending_validation || stats.pendingValidation || 0);
                setElementText('stat-vector-count', stats.vector_count || stats.vector_synced_count || 0);
            }
        } catch (error) {
            console.error('加载统计失败:', error);
        }
    },
    
    async loadKnowledge() {
        try {
            const params = new URLSearchParams({
                page: this.state.currentPage,
                page_size: this.state.pageSize
            });
            
            const searchEl = document.getElementById('search-knowledge');
            const search = searchEl ? searchEl.value : '';

            if (search) params.append('search', search);
            
            const response = await knowledgeFetchWithTimeout('/api/knowledge?' + params);
            const data = await response.json();
            
            if (data.items || data.data) {
                this.state.knowledge = data.items || (data.data && data.data.items) || [];
                this.state.totalPages = Math.ceil((data.total || (data.data && data.data.total) || 0) / this.state.pageSize);
                
                this.renderKnowledgeList();
                this.renderPagination();
                this.updateKnowledgeCount();
            }
        } catch (error) {
            console.error('加载知识库失败:', error);
            this.showToast('加载知识库失败', 'error');
        }
    },
    
    renderKnowledgeList() {
        this.renderTableView();
    },
    
    renderTableView() {
        const tbody = document.getElementById('knowledge-list');
        if (!tbody) return;
        tbody.innerHTML = '';
        
        if (this.state.knowledge.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">暂无知识数据</td></tr>';
            this.syncKnowledgeSelectionState();
            return;
        }
        
        this.state.knowledge.forEach(item => {
            const schemaId = this.getKnowledgeSchemaId(item);
            const answerText = String(item.answer || '');
            const answerPreview = answerText.length > 120 ? `${answerText.substring(0, 120)}...` : answerText;
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><input type="checkbox" class="knowledge-checkbox" data-id="${this.escapeHtml(item.id)}" ${this.state.selectedKnowledge.has(item.id) ? 'checked' : ''} onchange="KnowledgeApp.toggleKnowledgeSelect(this.dataset.id)"></td>
                <td>
                    <div class="question-cell">
                        <div class="question-text">${this.escapeHtml(item.question || '')}</div>
                        <div class="d-flex flex-wrap gap-1 mt-1">${this.renderKnowledgeBadges(item)}</div>
                        <small class="text-muted question-answer-preview">${this.escapeHtml(answerPreview)}</small>
                    </div>
                </td>
                <td><span class="badge bg-info">${this.getCategoryLabel(item.category)}</span></td>
                <td>
                    <div class="small fw-bold">${this.getSchemaLabel(schemaId)}</div>
                    <div class="small text-muted">${this.getTopicLabel(item.topic)}</div>
                </td>
                <td>${item.usageCount || 0}</td>
                <td>
                    <div class="btn-group btn-group-sm">
                        <button class="btn btn-outline-primary btn-sm" data-id="${this.escapeHtml(item.id)}" onclick="KnowledgeApp.editKnowledge(this.dataset.id)" title="编辑">✏️</button>
                        <button class="btn btn-outline-danger btn-sm" data-id="${this.escapeHtml(item.id)}" onclick="KnowledgeApp.deleteKnowledge(this.dataset.id)" title="删除">🗑️</button>
                    </div>
                </td>
            `;
            tbody.appendChild(tr);
        });

        this.syncKnowledgeSelectionState();
    },
    
    renderCardView() {
        const container = document.getElementById('knowledge-card-view');
        if (!container) return;
        container.innerHTML = '';
        
        this.state.knowledge.forEach(item => {
            const schemaId = this.getKnowledgeSchemaId(item);
            const card = document.createElement('div');
            card.className = 'knowledge-card-item';
            card.innerHTML = `
                <div class="card-question">${this.escapeHtml(item.question || '')}</div>
                <div class="d-flex flex-wrap gap-1 mt-2">${this.renderKnowledgeBadges(item)}</div>
                <div class="card-answer">${this.escapeHtml((item.answer || '').substring(0, 100))}${(item.answer || '').length > 100 ? '...' : ''}</div>
                <div class="card-meta d-flex justify-content-between align-items-center mt-2">
                    <span class="badge bg-info">${this.getCategoryLabel(item.category)}</span>
                    <span class="small text-muted">${this.getSchemaLabel(schemaId)}</span>
                </div>
                <div class="small text-muted mt-1">
                    领域：${this.getEntityLabel(item.domain)} · 主题：${this.getTopicLabel(item.topic)}
                </div>
                <div class="card-actions mt-2 d-flex gap-1">
                    <button class="btn btn-sm btn-outline-primary" data-id="${this.escapeHtml(item.id)}" onclick="KnowledgeApp.editKnowledge(this.dataset.id)" title="编辑">✏️</button>
                    <button class="btn btn-sm btn-outline-danger" data-id="${this.escapeHtml(item.id)}" onclick="KnowledgeApp.deleteKnowledge(this.dataset.id)" title="删除">🗑️</button>
                </div>
            `;
            container.appendChild(card);
        });
    },
    
    renderPagination() {
        const pagination = document.getElementById('knowledge-pagination');
        const info = document.getElementById('knowledge-pagination-info');
        const prevBtn = document.getElementById('knowledge-prev-page');
        const nextBtn = document.getElementById('knowledge-next-page');
        
        if (info) info.textContent = `第 ${this.state.currentPage} 页 / 共 ${this.state.totalPages} 页`;
        
        if (prevBtn) prevBtn.className = `page-item ${this.state.currentPage <= 1 ? 'disabled' : ''}`;
        if (nextBtn) nextBtn.className = `page-item ${this.state.currentPage >= this.state.totalPages ? 'disabled' : ''}`;
    },
    
    updateKnowledgeCount() {
        const countEl = document.getElementById('knowledge-count');
        if (countEl) countEl.textContent = this.state.knowledge.length;
    },
    
    async loadUploadedFiles() {
        try {
            const params = new URLSearchParams();
            const categoryEl = document.getElementById('filter-file-category');
            const statusEl = document.getElementById('filter-file-status');
            this.ensurePendingOcrStatusOption(statusEl);
            
            if (categoryEl && categoryEl.value) params.append('category', categoryEl.value);
            if (statusEl && statusEl.value) params.append('status', statusEl.value);
            
            const response = await knowledgeFetchWithTimeout('/api/rag/files?' + params);
            const data = await response.json();
            
            if (data.files !== undefined || data.data) {
                this.state.uploadedFiles = data.files || data.data || [];
                this.renderUploadedFiles();
            }
        } catch (error) {
            console.error('加载文件列表失败:', error);
        }
    },
    
    renderUploadedFiles() {
        const tbody = document.getElementById('uploaded-files-list');
        if (!tbody) return;
        tbody.innerHTML = '';
        
        if (this.state.uploadedFiles.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-4">暂无上传文件</td></tr>';
            return;
        }
        
        let totalSize = 0;
        let totalChunks = 0;
        let totalVectors = 0;
        
        this.state.uploadedFiles.forEach(file => {
            totalSize += file.size || 0;
            totalChunks += file.chunk_count || file.chunkCount || 0;
            totalVectors += file.vector_count || file.vectorCount || 0;
            
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><input type="checkbox" class="file-checkbox" data-id="${this.escapeHtml(file.id)}" onchange="KnowledgeApp.toggleFileSelect(this.dataset.id)"></td>
                <td>
                    <div class="file-name-cell">
                        <span class="file-icon">📄</span>
                        ${this.escapeHtml(file.filename || file.name || '')}
                    </div>
                </td>
                <td>${this.formatFileSize(file.size)}</td>
                <td><span class="badge bg-info">${this.getCategoryLabel(file.category)}</span></td>
                <td>${file.chunk_count || file.chunkCount || 0}</td>
                <td>
                    <span class="status-badge status-${file.status || 'uploaded'}">
                        ${this.escapeHtml(file.status_label || this.getFileStatusText(file.status))}
                    </span>
                </td>
                <td>${this.formatDate(file.upload_time || file.uploadTime || file.created_at)}</td>
                <td>
                    <div class="btn-group btn-group-sm">
                        <button class="btn btn-outline-primary btn-sm" data-id="${this.escapeHtml(file.id)}" data-enterprise-id="${this.escapeHtml(file.enterpriseId || file.enterprise_id || 'default')}" onclick="KnowledgeApp.processFile(this.dataset.id, this.dataset.enterpriseId)" title="处理">⚙️</button>
                        <button class="btn btn-outline-info btn-sm" data-id="${this.escapeHtml(file.id)}" onclick="KnowledgeApp.previewFile(this.dataset.id)" title="预览">👁️</button>
                        <button class="btn btn-outline-danger btn-sm" data-id="${this.escapeHtml(file.id)}" onclick="KnowledgeApp.deleteFile(this.dataset.id)" title="删除">🗑️</button>
                    </div>
                </td>
            `;
            tbody.appendChild(tr);
        });
        
        const setElementText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        setElementText('uploaded-files-count', this.state.uploadedFiles.length);
        setElementText('total-files-size', this.formatFileSize(totalSize));
        setElementText('total-chunks', totalChunks);
        setElementText('total-vectors', totalVectors);
    },

    renderStructuringEnterpriseOptions() {
        const select = document.getElementById('doc-structuring-enterprise');
        if (!select) return;
        const currentValue = String(select.value || this.state.enterprises?.[0]?.id || 'default').trim() || 'default';
        const options = (this.state.enterprises || []).map(item => {
            const id = String(item.id || '').trim();
            if (!id) return null;
            const label = String(item.name || item.display_name || id).trim();
            return [id, `${label} (${id})`];
        }).filter(Boolean);
        if (!options.some(([value]) => value === 'default')) {
            options.unshift(['default', '默认企业 (default)']);
        }
        this.renderSelectOptions('doc-structuring-enterprise', options, currentValue);
    },

    resetKnowledgeSourceSummary() {
        const wrapperEl = document.getElementById('knowledge-source-summary');
        const contentEl = document.getElementById('knowledge-source-summary-content');
        const openBtnEl = document.getElementById('knowledge-source-open-btn');
        if (contentEl) contentEl.textContent = '当前知识暂无来源信息。';
        if (wrapperEl) wrapperEl.classList.add('d-none');
        if (openBtnEl) {
            openBtnEl.classList.add('d-none');
            openBtnEl.dataset.documentSourceId = '';
            openBtnEl.dataset.draftId = '';
            openBtnEl.dataset.enterpriseId = '';
        }
    },

    renderKnowledgeSourceSummary(item) {
        const wrapperEl = document.getElementById('knowledge-source-summary');
        const contentEl = document.getElementById('knowledge-source-summary-content');
        const openBtnEl = document.getElementById('knowledge-source-open-btn');
        if (!wrapperEl || !contentEl) return;
        const metadata = item?.metadata || {};
        const hasSource = (
            String(item?.source || '').trim() === 'document_structuring' ||
            String(metadata.source || '').trim() === 'document_structuring' ||
            String(metadata.document_source_id || '').trim() ||
            String(metadata.draft_id || '').trim()
        );
        if (!hasSource) {
            this.resetKnowledgeSourceSummary();
            return;
        }
        const rows = [
            `知识ID：${this.escapeHtml(item?.id || '-')}`,
            `来源方式：${this.escapeHtml(String(metadata.source || item?.source || 'document_structuring'))}`,
            `来源文档：${this.escapeHtml(metadata.original_name || '-')}`,
            `文档ID：${this.escapeHtml(metadata.document_source_id || '-')}`,
            `草稿ID：${this.escapeHtml(metadata.draft_id || '-')}`,
            `位置：第 ${this.escapeHtml(String(metadata.source_page_no || 0))} 页 · ${this.escapeHtml(metadata.source_section || '未分段')}`
        ];
        contentEl.innerHTML = rows.map(line => `<div>${line}</div>`).join('');
        wrapperEl.classList.remove('d-none');
        if (openBtnEl) {
            openBtnEl.dataset.documentSourceId = String(metadata.document_source_id || '').trim();
            openBtnEl.dataset.draftId = String(metadata.draft_id || '').trim();
            openBtnEl.dataset.enterpriseId = String(item?.enterprise_id || 'default').trim() || 'default';
            openBtnEl.classList.toggle('d-none', !openBtnEl.dataset.documentSourceId);
        }
    },

    populateKnowledgeModal(item, options = {}) {
        if (!item) return;
        const titleEl = document.getElementById('knowledgeModalTitle');
        const modalEl = document.getElementById('knowledgeModal');
        if (titleEl) titleEl.textContent = options.title || '编辑知识';
        const setElementValue = (elementId, value) => {
            const el = document.getElementById(elementId);
            if (el) el.value = value;
        };
        this.renderKnowledgeTaxonomyOptions(item.category || 'other', item.domain || 'generic');
        setElementValue('knowledge-id', item.id);
        setElementValue('knowledge-enterprise', item.enterprise_id || 'default');
        setElementValue('knowledge-question', item.question || '');
        setElementValue('knowledge-answer', item.answer || '');
        setElementValue('knowledge-category', item.category || 'other');
        setElementValue('knowledge-domain', item.domain || 'generic');
        setElementValue('knowledge-topic', item.topic || '');
        setElementValue('knowledge-priority', item.priority || 0);
        setElementValue('knowledge-keywords', (item.keywords || []).join(', '));
        setElementValue('knowledge-aliases', (item.aliases || []).join(', '));
        setElementValue('knowledge-templates', (item.reply_templates || []).join('\n'));
        setElementValue('knowledge-metadata', JSON.stringify(item.metadata || {}, null, 2));
        this.renderKnowledgeSourceSummary(item);
        if (modalEl) {
            const modal = new bootstrap.Modal(modalEl);
            modal.show();
        }
    },

    getStructuringEnterpriseId() {
        const select = document.getElementById('doc-structuring-enterprise');
        return String(select?.value || 'default').trim() || 'default';
    },

    getStructuringStatusLabel(status) {
        const labels = {
            pending: '待处理',
            queued: '已排队',
            processing: '处理中',
            completed: '已完成',
            failed: '失败',
            pending_ocr: '待OCR',
            draft_only: '仅草稿',
            pending_publish: '待发布',
            published: '已发布',
            approved: '已通过',
            rejected: '已拒绝',
            edited: '已编辑',
            uploaded: '已上传'
        };
        return labels[String(status || '').trim()] || (status || '-');
    },

    getStructuringStatusBadgeClass(status) {
        const mapping = {
            pending: 'bg-light text-dark border',
            queued: 'bg-info-subtle text-info border',
            processing: 'bg-primary-subtle text-primary border',
            completed: 'bg-success-subtle text-success border',
            failed: 'bg-danger-subtle text-danger border',
            pending_ocr: 'bg-warning-subtle text-warning border',
            draft_only: 'bg-secondary-subtle text-secondary border',
            pending_publish: 'bg-warning-subtle text-warning border',
            published: 'bg-success-subtle text-success border',
            approved: 'bg-success-subtle text-success border',
            rejected: 'bg-danger-subtle text-danger border',
            edited: 'bg-primary-subtle text-primary border',
            uploaded: 'bg-light text-dark border'
        };
        return mapping[String(status || '').trim()] || 'bg-light text-dark border';
    },

    getStructuringChangeTypeLabel(changeType) {
        const labels = {
            update: '编辑',
            approve: '通过',
            reject: '拒绝',
            publish: '发布'
        };
        return labels[String(changeType || '').trim()] || (changeType || '-');
    },

    async reloadStructuringWorkspace() {
        this.state.initializedWorkspaces.delete('structuring');
        await this.loadWorkspaceData('structuring', { forceReload: true });
    },

    openStructuringFilePicker() {
        const input = document.getElementById('doc-structuring-upload-input');
        if (!input) return;
        input.value = '';
        input.click();
    },

    bindStructuringUploadInput() {
        if (this._structuringUploadBound) return;
        this._structuringUploadBound = true;
        const input = document.getElementById('doc-structuring-upload-input');
        if (!input) return;
        input.addEventListener('change', async (event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            const enterpriseId = this.getStructuringEnterpriseId();
            const formData = new FormData();
            formData.append('file', file);
            formData.append('enterprise_id', enterpriseId);
            formData.append('category_hint', 'faq');
            formData.append('auto_parse', 'true');
            formData.append('auto_generate_drafts', 'true');
            formData.append('draft_only', 'true');
            try {
                this.showToast('文档上传中...', 'info');
                const response = await knowledgeFetchWithTimeout('/api/doc-structuring/upload', {
                    method: 'POST',
                    body: formData
                }, 60000);
                const result = await response.json();
                if (result.success) {
                    this.showToast('文档上传成功，已进入整理工作台', 'success');
                    await this.reloadStructuringWorkspace();
                } else {
                    this.showToast(result.message || '文档上传失败', 'error');
                }
            } catch (error) {
                this.showToast(`文档上传失败：${error.message}`, 'error');
            } finally {
                input.value = '';
            }
        });
    },

    async loadStructuringStats() {
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const params = new URLSearchParams({ enterprise_id: enterpriseId });
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/stats?${params.toString()}`);
            const payload = await response.json();
            const stats = payload.data || {};
            this.state.structuringStats = stats;
            const setText = (id, value) => {
                const el = document.getElementById(id);
                if (el) el.textContent = value;
            };
            setText('doc-structuring-stat-pending-parse', stats.pending_parse_count || 0);
            setText('doc-structuring-stat-pending-structuring', stats.pending_structuring_count || 0);
            setText('doc-structuring-stat-pending-review', stats.pending_review_count || 0);
            setText('doc-structuring-stat-published', stats.published_count || 0);
        } catch (error) {
            console.error('加载文档整理统计失败:', error);
        }
    },

    async loadStructuringDocuments() {
        try {
            this.bindStructuringUploadInput();
            this.renderStructuringEnterpriseOptions();
            const enterpriseId = this.getStructuringEnterpriseId();
            const keyword = String(document.getElementById('doc-structuring-search')?.value || '').trim();
            const params = new URLSearchParams({ enterprise_id: enterpriseId, page: '1', page_size: '100' });
            if (keyword) params.append('keyword', keyword);
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/documents?${params.toString()}`);
            const payload = await response.json();
            this.state.structuringDocuments = payload.data || [];
            this.renderStructuringDocumentList();
            const countEl = document.getElementById('doc-structuring-document-count');
            if (countEl) countEl.textContent = `${payload.pagination?.total || this.state.structuringDocuments.length || 0} 份`;
            const selectedExists = this.state.structuringDocuments.some(item => item.id === this.state.selectedStructuringDocumentId);
            if (!selectedExists) {
                this.state.selectedStructuringDocumentId = this.state.structuringDocuments[0]?.id || '';
            }
            if (this.state.selectedStructuringDocumentId) {
                await Promise.all([
                    this.loadStructuringDocumentDetail(this.state.selectedStructuringDocumentId),
                    this.loadStructuringDocumentHistory(this.state.selectedStructuringDocumentId),
                    this.loadStructuringDrafts(this.state.selectedStructuringDocumentId)
                ]);
            } else {
                this.renderStructuringDocumentDetail(null);
                this.renderStructuringHistoryPanel(null);
                this.renderStructuringDraftList([]);
            }
        } catch (error) {
            console.error('加载文档整理列表失败:', error);
            this.showToast(`加载文档列表失败：${error.message}`, 'error');
        }
    },

    renderStructuringDocumentList() {
        const container = document.getElementById('doc-structuring-document-list');
        if (!container) return;
        if (!this.state.structuringDocuments.length) {
            container.innerHTML = '<div class="text-center text-muted py-4">暂无文档</div>';
            return;
        }
        container.innerHTML = this.state.structuringDocuments.map(item => {
            const active = item.id === this.state.selectedStructuringDocumentId;
            return `
                <div class="list-group-item ${active ? 'active' : ''}">
                    <div class="d-flex justify-content-between align-items-start gap-2">
                        <button type="button" class="btn btn-link text-decoration-none text-start flex-grow-1 p-0 ${active ? 'text-white' : 'text-dark'}" onclick="KnowledgeApp.selectStructuringDocument('${this.escapeHtml(item.id)}')">
                            <div class="fw-bold">${this.escapeHtml(item.original_name || item.id || '-')}</div>
                            <div class="small ${active ? 'text-white-50' : 'text-muted'}">${this.escapeHtml(item.category_hint || 'other')} · ${this.formatRelativeDate(item.created_at)}</div>
                        </button>
                        <div class="d-flex align-items-center gap-2">
                            <span class="badge ${active ? 'bg-light text-dark' : this.getStructuringStatusBadgeClass(item.structuring_status)}">${this.escapeHtml(this.getStructuringStatusLabel(item.structuring_status || item.parse_status))}</span>
                            <button type="button" class="btn btn-sm ${active ? 'btn-light text-danger' : 'btn-outline-danger'}" onclick="KnowledgeApp.deleteStructuringDocument('${this.escapeHtml(item.id)}')">删除</button>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    },

    async selectStructuringDocument(documentId) {
        this.state.selectedStructuringDocumentId = documentId;
        this.state.selectedStructuringDraftId = '';
        this.state.selectedStructuringDrafts.clear();
        this.state.structuringHistoryExpanded = {
            drafts: false,
            published: false,
            timeline: false
        };
        this.switchStructuringSubtab('detail');
        this.renderStructuringDocumentList();
        await Promise.all([
            this.loadStructuringDocumentDetail(documentId),
            this.loadStructuringDocumentHistory(documentId),
            this.loadStructuringDrafts(documentId)
        ]);
    },

    async loadStructuringDocumentDetail(documentId) {
        if (!documentId) {
            this.renderStructuringDocumentDetail(null);
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const params = new URLSearchParams({ enterprise_id: enterpriseId });
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/documents/${encodeURIComponent(documentId)}?${params.toString()}`);
            const payload = await response.json();
            this.state.selectedStructuringDocumentDetail = payload.data || null;
            this.renderStructuringDocumentDetail(this.state.selectedStructuringDocumentDetail);
        } catch (error) {
            this.state.selectedStructuringDocumentDetail = null;
            this.renderStructuringDocumentDetail(null);
            this.showToast(`加载文档详情失败：${error.message}`, 'error');
        }
    },

    async loadStructuringDocumentHistory(documentId) {
        if (!documentId) {
            this.state.selectedStructuringDocumentHistory = null;
            this.renderStructuringHistoryPanel(null);
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const params = new URLSearchParams({ enterprise_id: enterpriseId });
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/documents/${encodeURIComponent(documentId)}/history?${params.toString()}`);
            const payload = await response.json();
            this.state.selectedStructuringDocumentHistory = payload.data || null;
            this.renderStructuringHistoryPanel(this.state.selectedStructuringDocumentHistory);
        } catch (error) {
            this.state.selectedStructuringDocumentHistory = null;
            this.renderStructuringHistoryPanel(null);
            this.showToast(`加载发布追踪失败：${error.message}`, 'error');
        }
    },

    renderStructuringDocumentDetail(detail) {
        const emptyEl = document.getElementById('doc-structuring-detail-empty');
        const panelEl = document.getElementById('doc-structuring-detail-panel');
        const parseBtn = document.getElementById('doc-structuring-parse-btn');
        const structureBtn = document.getElementById('doc-structuring-structure-btn');
        const deleteBtn = document.getElementById('doc-structuring-delete-current-btn');
        if (!detail) {
            if (emptyEl) emptyEl.classList.remove('hidden');
            if (panelEl) panelEl.classList.add('hidden');
            if (parseBtn) parseBtn.disabled = true;
            if (structureBtn) structureBtn.disabled = true;
            if (deleteBtn) deleteBtn.disabled = true;
            return;
        }
        if (emptyEl) emptyEl.classList.add('hidden');
        if (panelEl) panelEl.classList.remove('hidden');
        if (parseBtn) parseBtn.disabled = false;
        if (structureBtn) structureBtn.disabled = false;
        if (deleteBtn) deleteBtn.disabled = false;
        const setText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        setText('doc-structuring-detail-name', detail.original_name || '-');
        setText('doc-structuring-detail-meta', `${detail.enterprise_id || 'default'} · ${detail.file_type || '-'} · ${this.formatFileSize(detail.file_size || 0)}`);
        setText('doc-structuring-detail-status', this.getStructuringStatusLabel(detail.publish_status || detail.structuring_status || detail.parse_status));
        setText('doc-structuring-detail-parse', this.getStructuringStatusLabel(detail.parse_status));
        setText('doc-structuring-detail-structure', this.getStructuringStatusLabel(detail.structuring_status));
        setText('doc-structuring-detail-publish', this.getStructuringStatusLabel(detail.publish_status));
        const statusBadge = document.getElementById('doc-structuring-detail-status');
        if (statusBadge) {
            statusBadge.className = `badge ${this.getStructuringStatusBadgeClass(detail.publish_status || detail.structuring_status || detail.parse_status)}`;
        }
        const jobsEl = document.getElementById('doc-structuring-job-list');
        if (jobsEl) {
            const jobs = Array.isArray(detail.jobs) ? detail.jobs : [];
            jobsEl.innerHTML = jobs.length ? jobs.map(job => `
                <div class="d-flex justify-content-between gap-2">
                    <span>${this.escapeHtml(job.job_type || '-')} · ${this.escapeHtml(this.getStructuringStatusLabel(job.job_status || '-'))}</span>
                    <span>${this.escapeHtml(job.current_step || '-')}</span>
                </div>
            `).join('') : '暂无任务记录';
        }
        const artifactsEl = document.getElementById('doc-structuring-artifact-list');
        if (artifactsEl) {
            const artifacts = Array.isArray(detail.artifacts) ? detail.artifacts : [];
            artifactsEl.innerHTML = artifacts.length ? artifacts.map(item => `
                <div class="d-flex justify-content-between gap-2">
                    <span>${this.escapeHtml(item.artifact_type || '-')}</span>
                    <span>${this.escapeHtml(String(item.content_length || 0))} 字符</span>
                </div>
            `).join('') : '暂无解析产物';
        }
    },

    renderStructuringHistoryPanel(history) {
        const emptyEl = document.getElementById('doc-structuring-history-empty');
        const panelEl = document.getElementById('doc-structuring-history-panel');
        const setText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        const renderHistoryItems = (items, section, renderItem, emptyText, previewCount = 6) => {
            const list = Array.isArray(items) ? items : [];
            if (!list.length) {
                return `<div class="text-muted">${this.escapeHtml(emptyText)}</div>`;
            }
            const expanded = Boolean(this.state.structuringHistoryExpanded[section]);
            const visibleItems = expanded ? list : list.slice(0, previewCount);
            const remainCount = Math.max(list.length - visibleItems.length, 0);
            return `
                <div class="d-flex flex-column gap-2">
                    ${visibleItems.map(renderItem).join('')}
                    ${list.length > previewCount ? `
                        <button type="button" class="btn btn-sm btn-outline-secondary align-self-start" onclick="KnowledgeApp.toggleStructuringHistoryList('${section}')">
                            ${expanded ? '收起' : `展开更多（剩余 ${remainCount} 条）`}
                        </button>
                    ` : ''}
                </div>
            `;
        };
        if (!history) {
            if (emptyEl) emptyEl.classList.remove('hidden');
            if (panelEl) panelEl.classList.add('hidden');
            return;
        }
        if (emptyEl) emptyEl.classList.add('hidden');
        if (panelEl) panelEl.classList.remove('hidden');
        const summary = history.summary || {};
        setText('doc-structuring-history-total', summary.total_draft_count || 0);
        setText('doc-structuring-history-approved', summary.approved_count || 0);
        setText('doc-structuring-history-rejected', summary.rejected_count || 0);
        setText('doc-structuring-history-published', summary.published_count || 0);
        const drafts = Array.isArray(history.drafts) ? history.drafts : [];
        const publishedLinks = Array.isArray(history.published_links) ? history.published_links : [];
        const timelineItems = Array.isArray(history.timeline) ? history.timeline : [];
        const formatLoadedCount = (loadedCount, totalCount = 0) => {
            const safeLoaded = Number(loadedCount || 0);
            const safeTotal = Number(totalCount || 0);
            if (!safeTotal || safeLoaded === safeTotal) {
                return safeLoaded;
            }
            return `${safeLoaded}/${safeTotal}`;
        };
        setText('doc-structuring-history-drafts-count', formatLoadedCount(drafts.length, summary.total_draft_count));
        setText('doc-structuring-history-published-count', formatLoadedCount(publishedLinks.length, summary.published_count));
        setText('doc-structuring-history-timeline-count', timelineItems.length);
        const draftListEl = document.getElementById('doc-structuring-history-draft-list');
        if (draftListEl) {
            draftListEl.innerHTML = renderHistoryItems(
                drafts,
                'drafts',
                item => `
                <div class="border rounded p-2 bg-white">
                    <div class="d-flex justify-content-between align-items-start gap-2">
                        <div class="fw-semibold text-dark">${this.escapeHtml(item.question || item.id || '-')}</div>
                        <span class="badge ${this.getStructuringStatusBadgeClass(item.review_status)}">${this.escapeHtml(this.getStructuringStatusLabel(item.review_status))}</span>
                    </div>
                    <div class="small text-muted mt-1">草稿ID：${this.escapeHtml(item.id || '-')}</div>
                    <div class="small text-muted">更新时间：${this.escapeHtml(this.formatRelativeDate(item.updated_at))}</div>
                </div>
            `,
                '暂无草稿状态',
                8
            );
        }
        const publishedListEl = document.getElementById('doc-structuring-history-published-list');
        if (publishedListEl) {
            publishedListEl.innerHTML = renderHistoryItems(
                publishedLinks,
                'published',
                item => `
                <div class="border rounded p-2 bg-white">
                    <div class="fw-semibold text-dark">${this.escapeHtml(item.question || item.draft_id || '-')}</div>
                    <div class="small text-muted mt-1">知识ID：${this.escapeHtml(item.knowledge_item_id || '-')}</div>
                    <div class="small text-muted">来源草稿：${this.escapeHtml(item.draft_id || '-')}</div>
                    <div class="small text-muted">位置：第 ${this.escapeHtml(String(item.page_no || 0))} 页 · ${this.escapeHtml(item.section_title || '未分段')}</div>
                    <div class="small text-muted">发布时间：${this.escapeHtml(this.formatRelativeDate(item.published_at))}</div>
                    <div class="d-flex gap-2 mt-2">
                        <button type="button" class="btn btn-outline-primary btn-sm" onclick="KnowledgeApp.openKnowledgeById('${this.escapeHtml(item.knowledge_item_id || '')}')">查看知识</button>
                        <button type="button" class="btn btn-outline-secondary btn-sm" onclick="KnowledgeApp.openKnowledgeById('${this.escapeHtml(item.knowledge_item_id || '')}', true)">定位资产</button>
                    </div>
                </div>
            `,
                '暂无发布记录',
                5
            );
        }
        const timelineEl = document.getElementById('doc-structuring-history-timeline');
        if (timelineEl) {
            timelineEl.innerHTML = renderHistoryItems(
                timelineItems,
                'timeline',
                item => `
                <div class="border rounded p-2 bg-white">
                    <div class="d-flex justify-content-between align-items-start gap-2">
                        <span class="fw-semibold text-dark">${this.escapeHtml(this.getStructuringChangeTypeLabel(item.change_type))}</span>
                        <span class="badge ${this.getStructuringStatusBadgeClass(item.review_status)}">${this.escapeHtml(this.getStructuringStatusLabel(item.review_status))}</span>
                    </div>
                    <div class="small text-muted mt-1">${this.escapeHtml(item.question || item.draft_id || '-')}</div>
                    <div class="small text-muted">操作者：${this.escapeHtml(item.changed_by || 'system')} · ${this.escapeHtml(this.formatRelativeDate(item.created_at))}</div>
                    <div class="small text-muted">版本：V${this.escapeHtml(String(item.version_no || 0))}</div>
                </div>
            `,
                '暂无操作历史',
                6
            );
        }
    },

    async deleteStructuringDocument(documentId = '', documentName = '') {
        const targetId = String(documentId || this.state.selectedStructuringDocumentId || '').trim();
        if (!targetId) {
            this.showToast('请先选择文档', 'warning');
            return;
        }
        const target = this.state.structuringDocuments.find(item => item.id === targetId)
            || this.state.selectedStructuringDocumentDetail
            || {};
        const targetName = String(documentName || target.original_name || targetId).trim();
        if (!confirm(`确定删除文档“${targetName}”吗？相关草稿、发布映射和来源知识也会一起删除。`)) {
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const params = new URLSearchParams({ enterprise_id: enterpriseId });
            const response = await knowledgeFetchWithTimeout(
                `/api/doc-structuring/documents/${encodeURIComponent(targetId)}?${params.toString()}`,
                { method: 'DELETE' }
            );
            const payload = await response.json();
            if (payload.success) {
                if (this.state.selectedStructuringDocumentId === targetId) {
                    this.state.selectedStructuringDocumentId = '';
                    this.state.selectedStructuringDocumentDetail = null;
                    this.state.selectedStructuringDocumentHistory = null;
                    this.state.selectedStructuringDraftId = '';
                    this.state.selectedStructuringDrafts.clear();
                }
                this.showToast(payload.message || '文档删除成功', 'success');
                await Promise.all([
                    this.loadStructuringDocuments(),
                    this.loadStructuringStats(),
                    this.refreshTopStatistics()
                ]);
            } else {
                this.showToast(payload.message || '文档删除失败', 'error');
            }
        } catch (error) {
            this.showToast(`文档删除失败：${error.message}`, 'error');
        }
    },

    async parseSelectedStructuringDocument() {
        const documentId = this.state.selectedStructuringDocumentId;
        if (!documentId) {
            this.showToast('请先选择文档', 'warning');
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            this.showToast('文档解析中...', 'info');
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/documents/${encodeURIComponent(documentId)}/parse?enterprise_id=${encodeURIComponent(enterpriseId)}`, {
                method: 'POST'
            }, 60000);
            const payload = await response.json();
            if (payload.success) {
                this.showToast('文档解析完成', 'success');
                await Promise.all([
                    this.loadStructuringDocumentDetail(documentId),
                    this.loadStructuringDocumentHistory(documentId),
                    this.loadStructuringStats()
                ]);
            } else {
                this.showToast(payload.message || '文档解析失败', 'error');
            }
        } catch (error) {
            this.showToast(`文档解析失败：${error.message}`, 'error');
        }
    },

    async structureSelectedDocument() {
        const documentId = this.state.selectedStructuringDocumentId;
        if (!documentId) {
            this.showToast('请先选择文档', 'warning');
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            this.showToast('正在生成草稿...', 'info');
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/documents/${encodeURIComponent(documentId)}/structure?enterprise_id=${encodeURIComponent(enterpriseId)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    structurer_type: 'faq',
                    draft_only: true,
                    max_drafts: 500,
                    regenerate: true
                })
            }, 60000);
            const payload = await response.json();
            if (payload.success) {
                this.showToast(`草稿生成完成，共 ${payload.data?.draft_count || 0} 条`, 'success');
                await Promise.all([
                    this.loadStructuringDocumentDetail(documentId),
                    this.loadStructuringDocumentHistory(documentId),
                    this.loadStructuringDrafts(documentId),
                    this.loadStructuringStats()
                ]);
            } else {
                this.showToast(payload.message || '草稿生成失败', 'error');
            }
        } catch (error) {
            this.showToast(`草稿生成失败：${error.message}`, 'error');
        }
    },

    async loadStructuringDrafts(documentId = this.state.selectedStructuringDocumentId) {
        if (!documentId) {
            this.state.structuringDrafts = [];
            this.renderStructuringDraftList([]);
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const params = new URLSearchParams({
                enterprise_id: enterpriseId,
                document_id: documentId,
                page: '1',
                page_size: '100'
            });
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/drafts?${params.toString()}`);
            const payload = await response.json();
            this.state.structuringDrafts = payload.data || [];
            this.renderStructuringDraftList(this.state.structuringDrafts);
            const selectedExists = this.state.structuringDrafts.some(item => item.id === this.state.selectedStructuringDraftId);
            if (!selectedExists) {
                this.state.selectedStructuringDraftId = this.state.structuringDrafts[0]?.id || '';
            }
            this.syncStructuringDraftSelectionState();
            if (this.state.selectedStructuringDraftId) {
                await this.loadStructuringDraftDetail(this.state.selectedStructuringDraftId);
            } else {
                this.renderStructuringDraftEditor(null);
            }
        } catch (error) {
            this.showToast(`加载草稿失败：${error.message}`, 'error');
        }
    },

    renderStructuringDraftList(drafts = []) {
        const tbody = document.getElementById('doc-structuring-draft-list');
        if (!tbody) return;
        if (!drafts.length) {
            tbody.innerHTML = '<tr><td colspan="3" class="text-center text-muted py-4">暂无草稿</td></tr>';
            this.renderStructuringDraftEditor(null);
            this.syncStructuringDraftSelectionState();
            return;
        }
        tbody.innerHTML = drafts.map(item => `
            <tr class="${item.id === this.state.selectedStructuringDraftId ? 'table-active' : ''}">
                <td><input type="checkbox" class="doc-structuring-draft-checkbox" data-id="${this.escapeHtml(item.id)}" ${this.state.selectedStructuringDrafts.has(item.id) ? 'checked' : ''} onchange="KnowledgeApp.toggleStructuringDraftSelection(this.dataset.id)"></td>
                <td>
                    <button type="button" class="btn btn-link btn-sm text-start p-0 text-decoration-none" onclick="KnowledgeApp.selectStructuringDraft('${this.escapeHtml(item.id)}')">
                        <div class="fw-semibold text-dark">${this.escapeHtml(item.question || '-')}</div>
                        <div class="small text-muted">${this.escapeHtml((item.answer || '').substring(0, 48))}${(item.answer || '').length > 48 ? '...' : ''}</div>
                    </button>
                </td>
                <td><span class="badge ${this.getStructuringStatusBadgeClass(item.review_status)}">${this.escapeHtml(this.getStructuringStatusLabel(item.review_status))}</span></td>
            </tr>
        `).join('');
        this.syncStructuringDraftSelectionState();
    },

    async selectStructuringDraft(draftId) {
        this.state.selectedStructuringDraftId = draftId;
        this.switchStructuringSubtab('review');
        this.renderStructuringDraftList(this.state.structuringDrafts);
        await this.loadStructuringDraftDetail(draftId);
    },

    async loadStructuringDraftDetail(draftId) {
        if (!draftId) {
            this.renderStructuringDraftEditor(null);
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/drafts/${encodeURIComponent(draftId)}?enterprise_id=${encodeURIComponent(enterpriseId)}`);
            const payload = await response.json();
            this.renderStructuringDraftEditor(payload.data || null);
        } catch (error) {
            this.renderStructuringDraftEditor(null);
            this.showToast(`加载草稿详情失败：${error.message}`, 'error');
        }
    },

    renderStructuringDraftEditor(draft) {
        const emptyEl = document.getElementById('doc-structuring-draft-empty');
        const editorEl = document.getElementById('doc-structuring-draft-editor');
        if (!draft) {
            if (emptyEl) emptyEl.classList.remove('hidden');
            if (editorEl) editorEl.classList.add('hidden');
            return;
        }
        if (emptyEl) emptyEl.classList.add('hidden');
        if (editorEl) editorEl.classList.remove('hidden');
        const setValue = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.value = value;
        };
        setValue('doc-structuring-draft-question', draft.question || '');
        setValue('doc-structuring-draft-answer', draft.answer || '');
        setValue('doc-structuring-draft-category', draft.category || '');
        setValue('doc-structuring-draft-domain', draft.domain || '');
        setValue('doc-structuring-draft-topic', draft.topic || '');
        setValue('doc-structuring-draft-keywords', (draft.keywords || []).join(', '));
        setValue('doc-structuring-draft-tags', (draft.tags || []).join(', '));
        setValue('doc-structuring-draft-aliases', (draft.aliases || []).join(', '));
        setValue('doc-structuring-draft-review-comment', draft.review_comment || '');
        const sourceEl = document.getElementById('doc-structuring-draft-source');
        if (sourceEl) {
            sourceEl.textContent = `来源信息：${draft.document?.original_name || '-'} | 状态：${this.getStructuringStatusLabel(draft.review_status)} | 页码：${draft.source_page_no || 0} | 来源片段：${draft.source_excerpt || '-'}`;
        }
    },

    toggleStructuringDraftSelection(draftId) {
        if (this.state.selectedStructuringDrafts.has(draftId)) {
            this.state.selectedStructuringDrafts.delete(draftId);
        } else {
            this.state.selectedStructuringDrafts.add(draftId);
        }
        this.syncStructuringDraftSelectionState();
    },

    toggleStructuringDraftSelectAll() {
        const selectAllEl = document.getElementById('doc-structuring-select-all-drafts');
        const checked = Boolean(selectAllEl?.checked);
        this.state.selectedStructuringDrafts.clear();
        if (checked) {
            this.state.structuringDrafts.forEach(item => this.state.selectedStructuringDrafts.add(item.id));
        }
        this.renderStructuringDraftList(this.state.structuringDrafts);
    },

    syncStructuringDraftSelectionState() {
        const selectedCount = this.state.selectedStructuringDrafts.size;
        const countEl = document.getElementById('doc-structuring-selected-draft-count');
        if (countEl) countEl.textContent = String(selectedCount);
        const selectAllEl = document.getElementById('doc-structuring-select-all-drafts');
        if (selectAllEl) {
            const total = this.state.structuringDrafts.length;
            selectAllEl.checked = total > 0 && selectedCount === total;
            selectAllEl.indeterminate = selectedCount > 0 && selectedCount < total;
        }
        ['doc-structuring-approve-btn', 'doc-structuring-reject-btn', 'doc-structuring-publish-btn'].forEach(id => {
            const btn = document.getElementById(id);
            if (btn) btn.disabled = selectedCount === 0;
        });
    },

    getCurrentDraftEditorPayload() {
        const parseList = (id) => String(document.getElementById(id)?.value || '').split(',').map(item => item.trim()).filter(Boolean);
        return {
            question: String(document.getElementById('doc-structuring-draft-question')?.value || '').trim(),
            answer: String(document.getElementById('doc-structuring-draft-answer')?.value || '').trim(),
            category: String(document.getElementById('doc-structuring-draft-category')?.value || '').trim() || 'other',
            domain: String(document.getElementById('doc-structuring-draft-domain')?.value || '').trim(),
            topic: String(document.getElementById('doc-structuring-draft-topic')?.value || '').trim(),
            keywords: parseList('doc-structuring-draft-keywords'),
            tags: parseList('doc-structuring-draft-tags'),
            aliases: parseList('doc-structuring-draft-aliases'),
            review_comment: String(document.getElementById('doc-structuring-draft-review-comment')?.value || '').trim(),
            edited_by: 'knowledge-admin'
        };
    },

    async saveSelectedDraft() {
        const draftId = this.state.selectedStructuringDraftId;
        if (!draftId) {
            this.showToast('请先选择草稿', 'warning');
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/drafts/${encodeURIComponent(draftId)}?enterprise_id=${encodeURIComponent(enterpriseId)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this.getCurrentDraftEditorPayload())
            });
            const payload = await response.json();
            if (payload.success) {
                this.showToast('草稿保存成功', 'success');
                await Promise.all([
                    this.loadStructuringDrafts(),
                    this.loadStructuringDocumentHistory(this.state.selectedStructuringDocumentId),
                    this.loadStructuringStats()
                ]);
            } else {
                this.showToast(payload.message || '草稿保存失败', 'error');
            }
        } catch (error) {
            this.showToast(`草稿保存失败：${error.message}`, 'error');
        }
    },

    async approveSelectedDrafts() {
        await this.runStructuringDraftBatchAction('/api/doc-structuring/drafts/batch-approve', '草稿审核通过成功');
    },

    async rejectSelectedDrafts() {
        await this.runStructuringDraftBatchAction('/api/doc-structuring/drafts/batch-reject', '草稿拒绝成功');
    },

    async publishSelectedDrafts() {
        await this.runStructuringDraftBatchAction('/api/doc-structuring/drafts/batch-publish', '草稿批量发布成功', { publish: true });
    },

    async publishCurrentDraft() {
        const draftId = this.state.selectedStructuringDraftId;
        if (!draftId) {
            this.showToast('请先选择草稿', 'warning');
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const response = await knowledgeFetchWithTimeout(`/api/doc-structuring/drafts/${encodeURIComponent(draftId)}/publish?enterprise_id=${encodeURIComponent(enterpriseId)}&operator=knowledge-admin`, {
                method: 'POST'
            }, 60000);
            const payload = await response.json();
            if (payload.success) {
                this.showToast('当前草稿发布成功', 'success');
                await Promise.all([
                    this.loadStructuringDrafts(),
                    this.loadStructuringDocumentDetail(this.state.selectedStructuringDocumentId),
                    this.loadStructuringDocumentHistory(this.state.selectedStructuringDocumentId),
                    this.loadStructuringStats(),
                    this.loadKnowledge(),
                    this.refreshTopStatistics()
                ]);
            } else {
                this.showToast(payload.message || '草稿发布失败', 'error');
            }
        } catch (error) {
            this.showToast(`草稿发布失败：${error.message}`, 'error');
        }
    },

    async runStructuringDraftBatchAction(url, successMessage, options = {}) {
        const draftIds = Array.from(this.state.selectedStructuringDrafts);
        if (!draftIds.length) {
            this.showToast('请先选择草稿', 'warning');
            return;
        }
        try {
            const enterpriseId = this.getStructuringEnterpriseId();
            const response = await knowledgeFetchWithTimeout(`${url}?enterprise_id=${encodeURIComponent(enterpriseId)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    draft_ids: draftIds,
                    review_comment: String(document.getElementById('doc-structuring-draft-review-comment')?.value || '').trim(),
                    operator: 'knowledge-admin'
                })
            }, 60000);
            const payload = await response.json();
            const batchData = payload.data || {};
            const failedItems = Array.isArray(batchData.failed_items) ? batchData.failed_items : [];
            const failedCount = Number(batchData.failed_count || 0);
            const successCount = Number(batchData.count || 0);
            const hasBatchResult = Boolean(options.publish) || failedCount > 0 || successCount > 0;
            if (payload.success || hasBatchResult) {
                this.state.selectedStructuringDrafts.clear();
                await Promise.all([
                    this.loadStructuringDrafts(),
                    this.loadStructuringDocumentDetail(this.state.selectedStructuringDocumentId),
                    this.loadStructuringDocumentHistory(this.state.selectedStructuringDocumentId),
                    this.loadStructuringStats()
                ]);
                if (options.publish) {
                    await Promise.all([
                        this.loadKnowledge(),
                        this.refreshTopStatistics()
                    ]);
                }
            }
            if (payload.success) {
                this.showToast(successMessage, 'success');
            } else if (failedCount > 0) {
                const firstReason = String(failedItems[0]?.reason || payload.message || '操作失败').trim();
                const prefix = successCount > 0
                    ? `部分成功，成功 ${successCount} 条，失败 ${failedCount} 条`
                    : `批量发布失败，失败 ${failedCount} 条`;
                this.showToast(`${prefix}：${firstReason}`, successCount > 0 ? 'warning' : 'error');
            } else {
                this.showToast(payload.message || '操作失败', 'error');
            }
        } catch (error) {
            this.showToast(`操作失败：${error.message}`, 'error');
        }
    },
    
    getFileStatusText(status) {
        const texts = {
            pending: '⏳ 待处理',
            uploaded: '📋 待处理',
            processing: '⚙️ 处理中',
            completed: '✅ 已完成',
            processed: '✅ 已完成',
            pending_ocr: '🧾 待OCR',
            failed: '❌ 失败',
            cancelled: '🚫 已取消',
            timeout: '⏰ 超时'
        };
        return texts[status] || '⏳ 待处理';
    },

    ensurePendingOcrStatusOption(selectEl) {
        if (!selectEl) return;
        const hasOption = Array.from(selectEl.options || []).some(opt => String(opt.value || '').trim() === 'pending_ocr');
        if (hasOption) return;
        const option = document.createElement('option');
        option.value = 'pending_ocr';
        option.textContent = '待OCR';
        selectEl.appendChild(option);
    },
    
    async loadGraphStats() {
        try {
            const response = await knowledgeFetchWithTimeout('/api/graph/stats');
            const data = await response.json();
            
            const stats = data.data || data;
            const entityCountEl = document.getElementById('graph-entity-count');
            const relationCountEl = document.getElementById('graph-relation-count');
            if (entityCountEl) entityCountEl.textContent = stats.entity_count || stats.entityCount || 0;
            if (relationCountEl) relationCountEl.textContent = stats.relation_count || stats.relationCount || 0;
        } catch (error) {
            console.error('加载图谱统计失败:', error);
        }
    },
    
    showAddKnowledgeModal() {
        const titleEl = document.getElementById('knowledgeModalTitle');
        const formEl = document.getElementById('knowledge-form');
        const idEl = document.getElementById('knowledge-id');
        const modalEl = document.getElementById('knowledgeModal');
        
        if (titleEl) titleEl.textContent = '添加知识';
        if (formEl) formEl.reset();
        if (idEl) idEl.value = '';
        const domainEl = document.getElementById('knowledge-domain');
        const topicEl = document.getElementById('knowledge-topic');
        const metadataEl = document.getElementById('knowledge-metadata');
        const templatesEl = document.getElementById('knowledge-templates');
        this.renderKnowledgeTaxonomyOptions('generic', 'generic');
        if (domainEl) domainEl.value = 'generic';
        if (topicEl) topicEl.value = '';
        if (metadataEl) metadataEl.value = '{}';
        if (templatesEl) templatesEl.value = '';
        this.resetKnowledgeSourceSummary();
        
        if (modalEl) {
            const modal = new bootstrap.Modal(modalEl);
            modal.show();
        }
    },
    
    async editKnowledge(id) {
        const item = this.state.knowledge.find(k => k.id === id);
        if (!item) return;
        this.populateKnowledgeModal(item, { title: '编辑知识' });
    },

    async openKnowledgeById(id, switchWorkspace = false) {
        const knowledgeId = String(id || '').trim();
        if (!knowledgeId) {
            this.showToast('缺少知识ID', 'warning');
            return;
        }
        try {
            let item = this.state.knowledge.find(k => k.id === knowledgeId);
            if (!item) {
                const response = await knowledgeFetchWithTimeout(`/api/knowledge/${encodeURIComponent(knowledgeId)}`);
                const payload = await response.json();
                item = payload.item || null;
            }
            if (!item) {
                this.showToast('未找到对应知识', 'warning');
                return;
            }
            if (switchWorkspace) {
                this.state.currentWorkspace = 'assets';
                localStorage.setItem('kb-workspace', 'assets');
                this.state.currentPage = 1;
                const searchEl = document.getElementById('search-knowledge');
                if (searchEl) searchEl.value = item.question || '';
                this.applyWorkspaceVisibility();
                this.state.initializedWorkspaces.delete('assets');
                await this.loadWorkspaceData('assets', { forceReload: true });
            }
            this.populateKnowledgeModal(item, { title: '查看知识' });
        } catch (error) {
            this.showToast(`加载知识详情失败：${error.message}`, 'error');
        }
    },

    async openKnowledgeSourceDocumentFromModal() {
        const openBtnEl = document.getElementById('knowledge-source-open-btn');
        const documentSourceId = String(openBtnEl?.dataset.documentSourceId || '').trim();
        const draftId = String(openBtnEl?.dataset.draftId || '').trim();
        const enterpriseId = String(openBtnEl?.dataset.enterpriseId || 'default').trim() || 'default';
        if (!documentSourceId) {
            this.showToast('当前知识没有来源文档信息', 'warning');
            return;
        }
        try {
            await this.loadCommonMetadata();
            this.state.currentWorkspace = 'structuring';
            localStorage.setItem('kb-workspace', 'structuring');
            this.state.currentStructuringSubtab = draftId ? 'review' : 'detail';
            localStorage.setItem('kb-structuring-subtab', this.state.currentStructuringSubtab);
            this.applyWorkspaceVisibility();
            this.renderStructuringEnterpriseOptions();
            const enterpriseSelect = document.getElementById('doc-structuring-enterprise');
            if (enterpriseSelect) {
                enterpriseSelect.value = enterpriseId;
            }
            this.state.selectedStructuringDocumentId = documentSourceId;
            this.state.selectedStructuringDraftId = draftId;
            this.state.selectedStructuringDrafts.clear();
            this.state.initializedWorkspaces.delete('structuring');
            const modalEl = document.getElementById('knowledgeModal');
            if (modalEl) {
                bootstrap.Modal.getOrCreateInstance(modalEl).hide();
            }
            await this.loadWorkspaceData('structuring', { forceReload: true });
            await this.selectStructuringDocument(documentSourceId);
            if (draftId) {
                await this.selectStructuringDraft(draftId);
            }
            this.showToast('已定位到来源文档', 'success');
        } catch (error) {
            this.showToast(`打开来源文档失败：${error.message}`, 'error');
        }
    },
    
    async saveKnowledge() {
        const getElementValue = (elementId, defaultValue = '') => {
            const el = document.getElementById(elementId);
            return el ? el.value : defaultValue;
        };
        
        const id = getElementValue('knowledge-id');
        let metadata = {};
        try {
            metadata = JSON.parse(getElementValue('knowledge-metadata', '{}') || '{}');
        } catch (error) {
            this.showToast('元数据 JSON 格式不正确', 'warning');
            return;
        }
        const data = {
            question: getElementValue('knowledge-question'),
            answer: getElementValue('knowledge-answer'),
            category: getElementValue('knowledge-category', 'other'),
            domain: getElementValue('knowledge-domain', 'generic'),
            topic: getElementValue('knowledge-topic'),
            priority: parseInt(getElementValue('knowledge-priority', '0')) || 0,
            keywords: getElementValue('knowledge-keywords').split(',').map(k => k.trim()).filter(k => k),
            aliases: getElementValue('knowledge-aliases').split(',').map(a => a.trim()).filter(a => a),
            reply_templates: getElementValue('knowledge-templates').split('\n').map(t => t.trim()).filter(t => t),
            metadata: metadata,
            enterprise_id: getElementValue('knowledge-enterprise', 'default') || 'default'
        };
        
        try {
            const url = id ? `/api/knowledge/${id}` : '/api/knowledge';
            const method = id ? 'PUT' : 'POST';
            
            const response = await knowledgeFetchWithTimeout(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
            
            const result = await response.json();
            
            if (result.success) {
                this.showToast(id ? '更新成功' : '添加成功', 'success');
                const modalEl = document.getElementById('knowledgeModal');
                if (modalEl) bootstrap.Modal.getInstance(modalEl)?.hide();
                this.loadKnowledge();
                this.loadStatistics();
            } else {
                this.showToast(result.message || '保存失败', 'error');
            }
        } catch (error) {
            this.showToast('保存失败: ' + error.message, 'error');
        }
    },

    getDefaultIndustrySchemaTemplate() {
        return {
            schema_id: 'new.industry_schema',
            version: 1,
            display_name: '新行业 Schema',
            industry_code: 'generic',
            entity_type: 'product',
            description: '',
            fields: [],
            intent_keywords: {},
            grounded_config: {
                bundle_type: 'entity',
                comparison_fields: [],
                followup_fields: []
            },
            metadata: {
                status: 'template',
                owner: 'system',
                category_profiles: {},
                query_understanding: {
                    domain_keywords: [],
                    business_intro_terms: [],
                    business_intro_patterns: [],
                    business_intro_exact_phrases: [],
                    followup_terms: [],
                    contextual_opening_guidance: []
                },
                intent_recognition: {
                    domain_context_terms: [],
                    domain_intent_augments: {},
                    domain_only_intent_patterns: {},
                    domain_entity_patterns: {},
                    domain_negation_rules: {},
                    domain_intent_inheritance: {},
                    domain_intent_entity_map: {},
                    domain_intent_descriptions: {},
                    domain_reasoning_names: {}
                },
                followup_strategy: {
                    domain_keywords: [],
                    generic_patterns: [],
                    anchor_terms: [],
                    short_followup_clues: [],
                    short_followup_leads: [],
                    required_slots: {},
                    slot_priority: {},
                    known_slot_terms: {},
                    signal_terms: {},
                    followup_intent_terms: {},
                    custom_rewrite_rules: [],
                    stage_cta_templates: {},
                    clarification_templates: {},
                    grounded_plan_closing_templates: {},
                    single_route_closing_templates: {}
                },
                slot_questions: {},
                reply_profile: {}
            }
        };
    },

    FIELD_PRESET_CATALOG: {
        generic: [
            { name: 'name', label: '名称', type: 'string', retrievable: true, comparable: true },
            { name: 'price', label: '价格', type: 'string', retrievable: true, comparable: true },
            { name: 'features', label: '特点', type: 'list', retrievable: true, comparable: true },
            { name: 'audience', label: '适用对象', type: 'list', retrievable: true, comparable: true }
        ],
        tourism: [
            { name: 'route_name', label: '线路名称', type: 'string', retrievable: true, comparable: true },
            { name: 'price', label: '价格', type: 'string', retrievable: true, comparable: true },
            { name: 'inclusions', label: '包含项目', type: 'list', retrievable: true, comparable: true },
            { name: 'schedule', label: '行程安排', type: 'string', retrievable: true, comparable: false },
            { name: 'pickup', label: '集合方式', type: 'string', retrievable: true, comparable: true },
            { name: 'audience', label: '适合人群', type: 'list', retrievable: true, comparable: true }
        ],
        education: [
            { name: 'name', label: '课程名称', type: 'string', retrievable: true, comparable: true },
            { name: 'tuition', label: '学费', type: 'string', retrievable: true, comparable: true },
            { name: 'audience', label: '适合对象', type: 'list', retrievable: true, comparable: true },
            { name: 'schedule', label: '上课安排', type: 'string', retrievable: true, comparable: true }
        ]
    },

    INTENT_PRESET_CATALOG: {
        generic: [
            { name: 'price', keywords: ['价格', '多少钱', '费用', '报价'] },
            { name: 'comparison', keywords: ['哪个好', '有什么区别', '怎么选'] },
            { name: 'booking', keywords: ['预约', '预订', '锁定', '下单', '购买'] },
            { name: 'details', keywords: ['介绍', '详情', '包含什么', '适合谁'] }
        ],
        tourism: [
            { name: 'price', keywords: ['价格', '多少钱', '费用', '报价'] },
            { name: 'comparison', keywords: ['哪个好', '有什么区别', '怎么选'] },
            { name: 'booking', keywords: ['报名', '预订', '锁位', '查余位'] },
            { name: 'details', keywords: ['包含什么', '行程', '集合方式', '适合谁'] }
        ],
        education: [
            { name: 'price', keywords: ['学费', '价格', '多少钱', '收费'] },
            { name: 'comparison', keywords: ['哪个班更合适', '有什么区别', '怎么选'] },
            { name: 'booking', keywords: ['报名', '预约试听', '锁定名额', '报班'] },
            { name: 'details', keywords: ['课程介绍', '适合谁', '上课安排', '课表'] }
        ]
    },

    BUNDLE_TYPE_OPTIONS: {
        entity: {
            title: '按商品/对象介绍',
            description: '适合课程、产品、套餐这类以单个对象为主的咨询场景。'
        },
        route: {
            title: '按线路/方案推荐',
            description: '适合旅游线路、多方案对比、需要从多个候选里帮用户选择的场景。'
        },
        service: {
            title: '按服务流程说明',
            description: '适合开通、办理、报名、售后这类以步骤和流程为主的场景。'
        }
    },

    splitCommaValues(value) {
        return String(value || '')
            .split(',')
            .map(item => item.trim())
            .filter(Boolean);
    },

    slugifySegment(value, fallback = '') {
        const normalized = String(value || '')
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '_')
            .replace(/^_+|_+$/g, '');
        return normalized || fallback;
    },

    buildAutoIndustrySchemaId(partial = {}) {
        const industry = this.slugifySegment(partial.industry_code || document.getElementById('industry-schema-industry-code')?.value || 'generic', 'generic');
        const entity = this.slugifySegment(partial.entity_type || document.getElementById('industry-schema-entity-type')?.value || 'service', 'service');
        return `${industry}.${entity}`;
    },

    getSchemaPresetKey(partial = {}) {
        const schemaId = String(partial.schema_id || document.getElementById('industry-schema-id')?.value || '').trim().toLowerCase();
        const industry = String(partial.industry_code || document.getElementById('industry-schema-industry-code')?.value || '').trim().toLowerCase();
        const entityType = String(partial.entity_type || document.getElementById('industry-schema-entity-type')?.value || '').trim().toLowerCase();
        const industryAliasMap = {
            tourism: ['tourism', '旅游', 'travel', 'route'],
            education: ['education', '教育', 'course', 'training']
        };
        if (schemaId.startsWith('tourism.')) return 'tourism';
        if (schemaId.startsWith('education.')) return 'education';
        if (industryAliasMap.tourism.includes(industry) || industryAliasMap.tourism.includes(entityType)) return 'tourism';
        if (industryAliasMap.education.includes(industry) || industryAliasMap.education.includes(entityType)) return 'education';
        return 'generic';
    },

    renderIndustrySchemaPresetButtons(schema = {}) {
        const presetKey = this.getSchemaPresetKey(schema);
        const fieldsContainer = document.getElementById('industry-schema-field-presets');
        const intentsContainer = document.getElementById('industry-schema-intent-presets');
        const activeFields = new Set((schema.fields || []).map(field => String(field.name || '').trim()).filter(Boolean));
        const activeIntents = new Set(Object.keys(schema.intent_keywords || {}).map(name => String(name || '').trim()).filter(Boolean));

        if (fieldsContainer) {
            const presets = this.FIELD_PRESET_CATALOG[presetKey] || this.FIELD_PRESET_CATALOG.generic;
            fieldsContainer.innerHTML = presets.map(field => `
                <button type="button" class="btn btn-sm ${activeFields.has(field.name) ? 'btn-success' : 'btn-outline-secondary'}" onclick="KnowledgeApp.addIndustrySchemaFieldFromPreset('${this.escapeHtml(field.name)}','${this.escapeHtml(field.label)}','${this.escapeHtml(field.type)}',${field.retrievable !== false},${field.comparable === true})">
                    ${activeFields.has(field.name) ? '已添加' : '+ 添加'} ${this.escapeHtml(field.label)}
                </button>
            `).join('');
        }

        if (intentsContainer) {
            const presets = this.INTENT_PRESET_CATALOG[presetKey] || this.INTENT_PRESET_CATALOG.generic;
            intentsContainer.innerHTML = presets.map(intent => `
                <div class="col-6">
                    <button type="button" class="btn btn-sm w-100 industry-schema-intent-preset-btn ${activeIntents.has(intent.name) ? 'btn-success' : 'btn-outline-secondary'}" onclick="KnowledgeApp.addIndustrySchemaIntentFromPreset('${this.escapeHtml(intent.name)}', decodeURIComponent('${encodeURIComponent(JSON.stringify(intent.keywords))}'))">
                        ${activeIntents.has(intent.name) ? '已添加' : '+ 添加'} ${this.escapeHtml(intent.name)}
                    </button>
                </div>
            `).join('');
        }
    },

    renderIndustrySchemaBundleTypeCards(bundleType = 'entity') {
        const container = document.getElementById('industry-schema-bundle-type-cards');
        const selectEl = document.getElementById('industry-schema-bundle-type');
        if (!container) return;
        const activeType = String(bundleType || selectEl?.value || 'entity').trim() || 'entity';
        if (selectEl) {
            selectEl.value = activeType;
        }
        container.innerHTML = Object.entries(this.BUNDLE_TYPE_OPTIONS).map(([value, option]) => `
            <div class="col-md-4">
                <button
                    type="button"
                    class="btn w-100 text-start ${activeType === value ? 'btn-primary' : 'btn-outline-secondary'}"
                    onclick="KnowledgeApp.selectIndustrySchemaBundleType('${value}')"
                >
                    <div class="fw-bold small">${this.escapeHtml(option.title)}</div>
                    <div class="small mt-1 ${activeType === value ? 'text-white-50' : 'text-muted'}">${this.escapeHtml(option.description)}</div>
                </button>
            </div>
        `).join('');
    },

    selectIndustrySchemaBundleType(bundleType) {
        const selectEl = document.getElementById('industry-schema-bundle-type');
        if (selectEl) {
            selectEl.value = bundleType;
        }
        this.renderIndustrySchemaBundleTypeCards(bundleType);
        this.handleIndustrySchemaFormChanged();
    },

    getResolvedIndustrySchemaId(partial = {}) {
        const manualToggle = document.getElementById('industry-schema-id-manual');
        const manualInput = document.getElementById('industry-schema-id-manual-input');
        if (manualToggle?.checked) {
            const manualId = String(manualInput?.value || partial.schema_id || '').trim();
            if (manualId) return manualId;
        }
        const storedId = String(document.getElementById('industry-schema-id')?.value || partial.schema_id || '').trim();
        if (storedId && storedId.includes('.')) {
            return storedId;
        }
        return this.buildAutoIndustrySchemaId(partial);
    },

    syncIndustrySchemaIdDisplay(partial = {}) {
        const resolvedId = this.getResolvedIndustrySchemaId(partial);
        const hiddenIdEl = document.getElementById('industry-schema-id');
        const previewEl = document.getElementById('industry-schema-id-preview');
        if (hiddenIdEl) hiddenIdEl.value = resolvedId;
        if (previewEl) previewEl.textContent = resolvedId || '系统会自动生成';
    },

    setIndustrySchemaFormValue(id, value) {
        const el = document.getElementById(id);
        if (el) el.value = value == null ? '' : String(value);
    },

    renderIndustrySchemaFieldsEditor(fields = []) {
        const container = document.getElementById('industry-schema-fields-editor');
        if (!container) return;
        if (!fields.length) {
            container.innerHTML = '<div class="small text-muted">还没有字段。建议至少配置名称、价格、包含项、适合人群这些核心信息。</div>';
            return;
        }
        container.innerHTML = fields.map((field, index) => `
            <div class="border rounded p-2 industry-schema-field-row" data-index="${index}">
                <div class="row g-2 align-items-center">
                    <div class="col-md-3">
                        <input type="text" class="form-control form-control-sm industry-schema-field-name" placeholder="系统字段名，如 price" value="${this.escapeHtml(field.name || '')}">
                    </div>
                    <div class="col-md-3">
                        <input type="text" class="form-control form-control-sm industry-schema-field-label" placeholder="页面显示名，如 价格" value="${this.escapeHtml(field.label || '')}">
                    </div>
                    <div class="col-md-2">
                        <select class="form-select form-select-sm industry-schema-field-type">
                            ${[
                                ['string', '文本'],
                                ['list', '列表'],
                                ['number', '数字'],
                                ['boolean', '是/否']
                            ].map(([type, label]) => `<option value="${type}" ${field.type === type ? 'selected' : ''}>${label}</option>`).join('')}
                        </select>
                    </div>
                    <div class="col-md-2">
                        <div class="form-check form-check-inline">
                            <input class="form-check-input industry-schema-field-retrievable" type="checkbox" ${field.retrievable !== false ? 'checked' : ''}>
                            <label class="form-check-label small">可检索</label>
                        </div>
                        <div class="form-check form-check-inline">
                            <input class="form-check-input industry-schema-field-comparable" type="checkbox" ${field.comparable ? 'checked' : ''}>
                            <label class="form-check-label small">可对比</label>
                        </div>
                    </div>
                    <div class="col-md-2 text-end">
                        <button class="btn btn-sm btn-outline-danger" onclick="KnowledgeApp.removeIndustrySchemaField(${index})">删除</button>
                    </div>
                </div>
            </div>
        `).join('');
    },

    renderIndustrySchemaIntentsEditor(intents = {}) {
        const container = document.getElementById('industry-schema-intents-editor');
        if (!container) return;
        const entries = Object.entries(intents || {});
        if (!entries.length) {
            container.innerHTML = '<div class="small text-muted">还没有常见问题分类。建议先配价格、对比、流程、详情等。</div>';
            return;
        }
        container.innerHTML = entries.map(([intent, keywords], index) => `
            <div class="border rounded p-2 industry-schema-intent-row" data-index="${index}">
                <div class="row g-2 align-items-center">
                    <div class="col-md-3">
                        <input type="text" class="form-control form-control-sm industry-schema-intent-name" placeholder="问题类型，如 price" value="${this.escapeHtml(intent || '')}">
                    </div>
                    <div class="col-md-7">
                        <input type="text" class="form-control form-control-sm industry-schema-intent-keywords" placeholder="用户常见问法，用逗号分隔" value="${this.escapeHtml((Array.isArray(keywords) ? keywords : []).join(', '))}">
                    </div>
                    <div class="col-md-2 text-end">
                        <button class="btn btn-sm btn-outline-danger" onclick="KnowledgeApp.removeIndustrySchemaIntent(${index})">删除</button>
                    </div>
                </div>
            </div>
        `).join('');
    },

    populateIndustrySchemaForm(item = {}) {
        const schema = JSON.parse(JSON.stringify(item || this.getDefaultIndustrySchemaTemplate()));
        this.state.currentIndustrySchema = schema;
        this.renderIndustrySchemaDatalists();
        const autoGeneratedId = this.buildAutoIndustrySchemaId(schema);
        const useManualId = Boolean(schema.schema_id && schema.schema_id !== autoGeneratedId);
        const manualIdToggle = document.getElementById('industry-schema-id-manual');
        const manualIdInput = document.getElementById('industry-schema-id-manual-input');
        if (manualIdToggle) manualIdToggle.checked = useManualId;
        if (manualIdInput) {
            manualIdInput.disabled = !useManualId;
            manualIdInput.value = useManualId ? (schema.schema_id || '') : '';
        }
        this.setIndustrySchemaFormValue('industry-schema-id', useManualId ? (schema.schema_id || '') : autoGeneratedId);
        this.setIndustrySchemaFormValue('industry-schema-display-name', schema.display_name || '');
        this.setIndustrySchemaFormValue('industry-schema-industry-code', schema.industry_code || 'generic');
        this.setIndustrySchemaFormValue('industry-schema-entity-type', schema.entity_type || 'product');
        this.setIndustrySchemaFormValue('industry-schema-description', schema.description || '');
        this.setIndustrySchemaFormValue('industry-schema-bundle-type', (schema.grounded_config || {}).bundle_type || 'entity');
        this.setIndustrySchemaFormValue('industry-schema-comparison-fields', ((schema.grounded_config || {}).comparison_fields || []).join(', '));
        this.setIndustrySchemaFormValue('industry-schema-followup-fields', ((schema.grounded_config || {}).followup_fields || []).join(', '));
        this.renderIndustrySchemaBundleTypeCards((schema.grounded_config || {}).bundle_type || 'entity');
        this.renderIndustrySchemaFieldsEditor(Array.isArray(schema.fields) ? schema.fields : []);
        this.renderIndustrySchemaIntentsEditor(schema.intent_keywords || {});
        this.renderIndustrySchemaPresetButtons(schema);
        this.renderIndustrySchemaSummary(schema);
        this.renderKnowledgeTaxonomyOptions();
        this.syncIndustrySchemaIdDisplay(schema);
        this.syncIndustrySchemaJsonPreview();
    },

    collectIndustrySchemaFromForm() {
        const base = JSON.parse(JSON.stringify(this.state.currentIndustrySchema || this.state.industrySchemaTemplate || this.getDefaultIndustrySchemaTemplate()));
        const schemaId = this.getResolvedIndustrySchemaId(base);
        if (!schemaId) {
            throw new Error('请先填写行业和销售对象，系统才能自动生成模板编号');
        }

        const fieldRows = Array.from(document.querySelectorAll('#industry-schema-fields-editor .industry-schema-field-row'));
        const fields = fieldRows.map(row => ({
            name: (row.querySelector('.industry-schema-field-name')?.value || '').trim(),
            label: (row.querySelector('.industry-schema-field-label')?.value || '').trim(),
            type: (row.querySelector('.industry-schema-field-type')?.value || 'string').trim(),
            retrievable: row.querySelector('.industry-schema-field-retrievable')?.checked !== false,
            comparable: row.querySelector('.industry-schema-field-comparable')?.checked === true
        })).filter(field => field.name && field.label);

        const intentRows = Array.from(document.querySelectorAll('#industry-schema-intents-editor .industry-schema-intent-row'));
        const intentKeywords = {};
        intentRows.forEach(row => {
            const name = (row.querySelector('.industry-schema-intent-name')?.value || '').trim();
            const keywords = this.splitCommaValues(row.querySelector('.industry-schema-intent-keywords')?.value || '');
            if (name) intentKeywords[name] = keywords;
        });

        base.schema_id = schemaId;
        base.version = Number(base.version || 1) || 1;
        base.display_name = (document.getElementById('industry-schema-display-name')?.value || '').trim();
        base.industry_code = (document.getElementById('industry-schema-industry-code')?.value || 'generic').trim();
        base.entity_type = (document.getElementById('industry-schema-entity-type')?.value || 'product').trim();
        base.description = (document.getElementById('industry-schema-description')?.value || '').trim();
        base.fields = fields;
        base.intent_keywords = intentKeywords;
        base.grounded_config = {
            ...(base.grounded_config || {}),
            bundle_type: (document.getElementById('industry-schema-bundle-type')?.value || 'entity').trim(),
            comparison_fields: this.splitCommaValues(document.getElementById('industry-schema-comparison-fields')?.value || ''),
            followup_fields: this.splitCommaValues(document.getElementById('industry-schema-followup-fields')?.value || '')
        };
        base.metadata = {
            ...(base.metadata || {}),
            status: ((base.metadata || {}).status || 'template'),
            owner: ((base.metadata || {}).owner || 'system')
        };
        this.state.currentIndustrySchema = base;
        return base;
    },

    syncIndustrySchemaJsonPreview() {
        const jsonEl = document.getElementById('industry-schema-json');
        if (!jsonEl) return;
        try {
            const payload = this.collectIndustrySchemaFromForm();
            jsonEl.value = JSON.stringify(payload, null, 2);
            this.renderIndustrySchemaSummary(payload);
        } catch (_error) {
            // 表单尚未填写完整时不打断用户输入
        }
    },

    handleIndustrySchemaFormChanged() {
        this.syncIndustrySchemaIdDisplay();
        this.syncIndustrySchemaJsonPreview();
    },

    addIndustrySchemaField(field = null) {
        const schema = JSON.parse(JSON.stringify(this.state.currentIndustrySchema || this.getDefaultIndustrySchemaTemplate()));
        const next = field || { name: '', label: '', type: 'string', retrievable: true, comparable: false };
        schema.fields = Array.isArray(schema.fields) ? schema.fields : [];
        schema.fields.push(next);
        this.populateIndustrySchemaForm(schema);
    },

    addIndustrySchemaFieldFromPreset(name, label, type = 'string', retrievable = true, comparable = false) {
        const schema = JSON.parse(JSON.stringify(this.state.currentIndustrySchema || this.getDefaultIndustrySchemaTemplate()));
        schema.fields = Array.isArray(schema.fields) ? schema.fields : [];
        if (schema.fields.some(field => String(field.name || '').trim() === String(name || '').trim())) {
            this.showToast(`“${label || name}”已经添加过了`, 'info');
            return;
        }
        schema.fields.push({
            name: String(name || '').trim(),
            label: String(label || '').trim(),
            type: String(type || 'string').trim(),
            retrievable: retrievable !== false,
            comparable: comparable === true
        });
        this.populateIndustrySchemaForm(schema);
    },

    removeIndustrySchemaField(index) {
        const schema = JSON.parse(JSON.stringify(this.state.currentIndustrySchema || this.getDefaultIndustrySchemaTemplate()));
        schema.fields = (schema.fields || []).filter((_, idx) => idx !== index);
        this.populateIndustrySchemaForm(schema);
    },

    addIndustrySchemaIntent(name = '', keywords = []) {
        const schema = JSON.parse(JSON.stringify(this.state.currentIndustrySchema || this.getDefaultIndustrySchemaTemplate()));
        schema.intent_keywords = schema.intent_keywords || {};
        let targetName = name || 'new_intent';
        let suffix = 1;
        while (schema.intent_keywords[targetName]) {
            targetName = `new_intent_${suffix}`;
            suffix += 1;
        }
        schema.intent_keywords[targetName] = Array.isArray(keywords) ? keywords : [];
        this.populateIndustrySchemaForm(schema);
    },

    addIndustrySchemaIntentFromPreset(name, keywords = []) {
        const normalizedName = String(name || '').trim();
        if (!normalizedName) return;
        const schema = JSON.parse(JSON.stringify(this.state.currentIndustrySchema || this.getDefaultIndustrySchemaTemplate()));
        schema.intent_keywords = schema.intent_keywords || {};
        if (schema.intent_keywords[normalizedName]) {
            this.showToast(`“${normalizedName}”已经添加过了`, 'info');
            return;
        }
        let normalizedKeywords = keywords;
        if (typeof normalizedKeywords === 'string') {
            try {
                normalizedKeywords = JSON.parse(normalizedKeywords);
            } catch (_error) {
                normalizedKeywords = this.splitCommaValues(normalizedKeywords);
            }
        }
        schema.intent_keywords[normalizedName] = Array.isArray(normalizedKeywords) ? normalizedKeywords : [];
        this.populateIndustrySchemaForm(schema);
    },

    removeIndustrySchemaIntent(index) {
        const schema = JSON.parse(JSON.stringify(this.state.currentIndustrySchema || this.getDefaultIndustrySchemaTemplate()));
        const entries = Object.entries(schema.intent_keywords || {}).filter((_, idx) => idx !== index);
        schema.intent_keywords = Object.fromEntries(entries);
        this.populateIndustrySchemaForm(schema);
    },

    async loadIndustrySchemas(forceReload = false) {
        if (!forceReload && this.state.initializedWorkspaces.has('schema') && this.state.industrySchemas.length > 0) {
            this.renderIndustrySchemaList();
            return;
        }
        try {
            const response = await knowledgeFetchWithTimeout('/api/industry-schemas');
            const data = await response.json();
            this.state.industrySchemas = data.items || [];
            this.state.industrySchemaTemplate = data.template || null;
            this.populateIndustrySchemaSettingsForm(data.settings || {});
            this.renderIndustrySchemaDatalists();
            this.renderKnowledgeTaxonomyOptions();
            this.renderIndustrySchemaList();

            const currentId = (document.getElementById('industry-schema-id')?.value || '').trim();
            const targetId = currentId || this.state.activeIndustrySchemaId || (this.state.industrySchemas[0] && this.state.industrySchemas[0].schema_id);
            if (targetId) {
                await this.openIndustrySchema(targetId);
            }
        } catch (error) {
            console.error('加载行业 Schema 失败:', error);
            const validationEl = document.getElementById('industry-schema-validation');
            const summaryEl = document.getElementById('industry-schema-readable-summary');
            if (validationEl) validationEl.textContent = '接口暂时不可用，请检查服务是否已启动';
            if (summaryEl) summaryEl.textContent = '行业模板接口暂时不可用。通常是本地预览服务没有启动，或仍在使用旧进程。';
            this.showToast(`加载行业模板失败：${error.message}`, 'error');
        }
    },

    renderIndustrySchemaList() {
        const listEl = document.getElementById('industry-schema-list');
        const countEl = document.getElementById('industry-schema-count');
        const activeEl = document.getElementById('industry-schema-active');
        if (countEl) countEl.textContent = String(this.state.industrySchemas.length || 0);
        if (activeEl) activeEl.textContent = this.state.activeIndustrySchemaId || '-';
        if (!listEl) return;

        if (!this.state.industrySchemas.length) {
            listEl.innerHTML = '<div class="text-muted text-center py-3">暂无行业模板</div>';
            return;
        }

        listEl.innerHTML = this.state.industrySchemas.map(item => `
            <button type="button" class="list-group-item list-group-item-action ${item.schema_id === this.state.activeIndustrySchemaId ? 'active' : ''}" onclick="KnowledgeApp.openIndustrySchema('${this.escapeHtml(item.schema_id)}')">
                <div class="d-flex w-100 justify-content-between align-items-start">
                    <div class="text-start">
                        <div class="fw-bold">${this.escapeHtml(item.display_name || item.schema_id)}</div>
                        <div class="small opacity-75">${this.getIndustryLabel(item.industry_code || '')} · ${this.getEntityLabel(item.entity_type || '')}</div>
                    </div>
                    <span class="badge ${item.is_active ? 'bg-success' : 'bg-secondary'}">${item.is_active ? '启用中' : '可选'}</span>
                </div>
                <div class="small mt-1 text-start">
                    已配置 ${item.field_count || 0} 个重要信息项，可处理 ${item.intent_count || 0} 类常见问题
                </div>
                <div class="small text-start opacity-75">
                    ${this.escapeHtml(item.description || `模板编号：${item.schema_id || '-'}`)}
                </div>
            </button>
        `).join('');
    },

    renderIndustrySchemaSummary(item = {}) {
        const fields = Array.isArray(item.fields) ? item.fields : [];
        const intents = item.intent_keywords || {};
        const grounded = item.grounded_config || {};

        const setText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };

        setText('industry-schema-summary-industry', this.getIndustryLabel(item.industry_code || '-'));
        setText('industry-schema-summary-entity', this.getEntityLabel(item.entity_type || '-'));
        setText('industry-schema-summary-fields', String(fields.length || 0));
        setText('industry-schema-summary-intents', String(Object.keys(intents).length || 0));

        const readableSummary = document.getElementById('industry-schema-readable-summary');
        if (readableSummary) {
            const displayName = item.display_name || item.schema_id || '当前模板';
            readableSummary.textContent = `${displayName} 主要告诉系统：这类业务最重要的信息是什么、用户最常问什么，以及回答时先参考哪些内容。`;
        }

        const fieldSummaryEl = document.getElementById('industry-schema-field-summary');
        if (fieldSummaryEl) {
            if (fields.length) {
                fieldSummaryEl.innerHTML = fields.slice(0, 6).map(field => {
                    const label = this.escapeHtml(field.label || field.name || '-');
                    const type = this.escapeHtml(field.type || 'string');
                    return `<span class="badge bg-light text-dark border me-1 mb-1">${label} · ${type}</span>`;
                }).join('');
            } else {
                fieldSummaryEl.textContent = '当前还没有添加重要信息项。';
            }
        }

        const intentSummaryEl = document.getElementById('industry-schema-intent-summary');
        if (intentSummaryEl) {
            const intentEntries = Object.entries(intents);
            if (intentEntries.length) {
                intentSummaryEl.innerHTML = intentEntries.slice(0, 5).map(([intent, keywords]) => {
                    const samples = Array.isArray(keywords) ? keywords.slice(0, 3).join('、') : '';
                    return `<div class="mb-1"><strong>${this.escapeHtml(intent)}</strong>：${this.escapeHtml(samples || '未配置示例')}</div>`;
                }).join('');
            } else {
                intentSummaryEl.textContent = '当前还没有添加常见问题类型。';
            }
        }

        const groundedSummaryEl = document.getElementById('industry-schema-grounded-summary');
        if (groundedSummaryEl) {
            const parts = [];
            if (grounded.bundle_type) parts.push(`会按“${grounded.bundle_type}”方式整理结构化证据`);
            if (Array.isArray(grounded.comparison_fields) && grounded.comparison_fields.length) {
                parts.push(`做推荐比较时重点参考 ${grounded.comparison_fields.slice(0, 5).join('、')}`);
            }
            if (Array.isArray(grounded.highlight_fields) && grounded.highlight_fields.length) {
                parts.push(`展示摘要时优先突出 ${grounded.highlight_fields.slice(0, 5).join('、')}`);
            }
            groundedSummaryEl.textContent = parts.length
                ? parts.join('；')
                : '当前模板还没有配置特殊的回答整理方式，系统会以普通问答为主。';
        }
    },

    async openIndustrySchema(schemaId) {
        if (!schemaId) return;
        try {
            const response = await knowledgeFetchWithTimeout(`/api/industry-schemas/${encodeURIComponent(schemaId)}`);
            const data = await response.json();
            const item = data.item || {};
            const validationEl = document.getElementById('industry-schema-validation');
            if (validationEl) validationEl.textContent = '已加载，可直接查看摘要或继续校验';
            this.populateIndustrySchemaForm(item);
        } catch (error) {
            this.showToast(`加载行业模板详情失败：${error.message}`, 'error');
        }
    },

    createIndustrySchemaFromTemplate() {
        const template = JSON.parse(JSON.stringify(this.state.industrySchemaTemplate || this.getDefaultIndustrySchemaTemplate()));
        const validationEl = document.getElementById('industry-schema-validation');
        if (validationEl) validationEl.textContent = '新模板已载入，请先填写行业和主要销售对象';
        this.populateIndustrySchemaForm(template);
        this.switchIndustrySchemaEditorMode('form');
    },

    createIndustrySchemaFromPreset(presetKey = 'generic') {
        const presetMap = {
            generic: 'generic.service_sales',
            tourism: 'tourism.route_sales',
            education: 'education.course_sales'
        };
        const presetSchemaId = presetMap[presetKey] || presetMap.generic;
        const existing = (this.state.industrySchemas || []).find(item => item.schema_id === presetSchemaId);
        if (existing) {
            this.openIndustrySchema(existing.schema_id);
            return;
        }
        const template = JSON.parse(JSON.stringify(this.state.industrySchemaTemplate || this.getDefaultIndustrySchemaTemplate()));
        template.industry_code = presetKey;
        if (presetKey === 'tourism') {
            template.entity_type = 'route';
            template.display_name = '旅游线路模板';
            template.description = '用于线路咨询、价格说明、行程对比和出行安排。';
        } else if (presetKey === 'education') {
            template.entity_type = 'course';
            template.display_name = '教育课程模板';
            template.description = '用于课程咨询、学费说明、试听安排和报名资料。';
        } else {
            template.entity_type = 'service';
            template.display_name = '通用销售模板';
            template.description = '用于通用产品或服务介绍、价格说明和流程解答。';
        }
        const validationEl = document.getElementById('industry-schema-validation');
        if (validationEl) validationEl.textContent = '已载入快捷预设，可直接按业务修改后保存';
        this.populateIndustrySchemaForm(template);
        this.switchIndustrySchemaEditorMode('form');
    },

    getIndustrySchemaPayload() {
        if (this.state.industrySchemaEditorMode === 'json') {
            const jsonEl = document.getElementById('industry-schema-json');
            const raw = jsonEl ? String(jsonEl.value || '').trim() : '';
            if (!raw) throw new Error('请先填写 Schema JSON');
            const payload = JSON.parse(raw);
            const schemaId = String(payload.schema_id || '').trim();
            if (!schemaId) throw new Error('Schema JSON 缺少 schema_id');
            this.state.currentIndustrySchema = payload;
            return { schemaId, payload };
        }
        const payload = this.collectIndustrySchemaFromForm();
        const schemaId = payload.schema_id;
        return { schemaId, payload };
    },

    async validateIndustrySchema() {
        try {
            const { payload } = this.getIndustrySchemaPayload();
            const response = await knowledgeFetchWithTimeout('/api/industry-schemas/validate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ schema: payload })
            });
            const result = await response.json();
            const validationEl = document.getElementById('industry-schema-validation');
            if (result.success && result.result) {
                const info = result.result;
                if (validationEl) validationEl.textContent = `校验通过：已识别 ${info.field_count || 0} 个字段、${info.intent_count || 0} 类常见问题`;
                this.showToast('行业模板校验通过', 'success');
            } else {
                if (validationEl) validationEl.textContent = result.message || '校验失败';
                this.showToast(result.message || '行业模板校验失败', 'error');
            }
        } catch (error) {
            const validationEl = document.getElementById('industry-schema-validation');
            if (validationEl) validationEl.textContent = error.message;
            this.showToast(`行业模板校验失败：${error.message}`, 'error');
        }
    },

    async saveIndustrySchema() {
        try {
            const { schemaId, payload } = this.getIndustrySchemaPayload();
            const response = await knowledgeFetchWithTimeout(`/api/industry-schemas/${encodeURIComponent(schemaId)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ schema: payload })
            });
            const result = await response.json();
            if (result.success) {
                this.showToast('行业模板已保存', 'success');
                await this.loadIndustrySchemas(true);
                this.switchIndustrySchemaEditorMode('form');
            } else {
                this.showToast(result.message || '行业模板保存失败', 'error');
            }
        } catch (error) {
            this.showToast(`行业模板保存失败：${error.message}`, 'error');
        }
    },

    async deleteIndustrySchema() {
        try {
            const { schemaId } = this.getIndustrySchemaPayload();
            if (!schemaId) {
                this.showToast('请先选择一个要删除的模板', 'warning');
                return;
            }
            if (!confirm(`确定删除模板“${schemaId}”吗？删除后无法恢复。`)) return;
            const response = await knowledgeFetchWithTimeout(`/api/industry-schemas/${encodeURIComponent(schemaId)}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            if (result.success) {
                this.showToast('模板已删除', 'success');
                await this.loadIndustrySchemas(true);
                this.createIndustrySchemaFromTemplate();
            } else {
                this.showToast(result.message || '删除失败', 'error');
            }
        } catch (error) {
            this.showToast(`删除失败：${error.message}`, 'error');
        }
    },

    async setActiveIndustrySchema() {
        try {
            const payload = this.collectIndustrySchemaSettingsPayload();
            const currentSchemaId = (document.getElementById('industry-schema-id')?.value || '').trim();
            if (currentSchemaId) {
                payload.active_schema_id = currentSchemaId;
            }
            if (!payload.active_schema_id) {
                this.showToast('请先选择或填写 Schema ID', 'warning');
                return;
            }
            const response = await knowledgeFetchWithTimeout('/api/industry-schemas/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await response.json();
            if (result.success) {
                this.populateIndustrySchemaSettingsForm(result.settings || payload);
                this.renderIndustrySchemaList();
                this.renderIndustrySchemaDatalists();
                this.showToast('行业 Schema 设置已更新', 'success');
            } else {
                this.showToast(result.message || '切换失败', 'error');
            }
        } catch (error) {
            this.showToast(`切换失败: ${error.message}`, 'error');
        }
    },
    
    async deleteKnowledge(id) {
        if (!confirm('确定要删除这条知识吗？')) return;
        
        try {
            const response = await knowledgeFetchWithTimeout(`/api/knowledge/${id}`, { method: 'DELETE' });
            const result = await response.json();
            
            if (result.success) {
                this.state.selectedKnowledge.delete(id);
                this.showToast('删除成功', 'success');
                await Promise.all([
                    this.loadKnowledge(),
                    this.refreshTopStatistics()
                ]);
            } else {
                this.showToast(result.message || '删除失败', 'error');
            }
        } catch (error) {
            this.showToast('删除失败', 'error');
        }
    },
    
    async deleteFile(id) {
        if (!confirm('确定要删除这个文件吗？相关的知识条目也会被删除。')) return;
        
        try {
            const response = await knowledgeFetchWithTimeout(`/api/rag/files/${id}`, { method: 'DELETE' });
            const result = await response.json();
            
            if (result.success) {
                this.state.selectedFiles.delete(id);
                this.showToast('删除成功', 'success');
                await Promise.all([
                    this.loadUploadedFiles(),
                    this.loadKnowledge(),
                    this.refreshTopStatistics()
                ]);
            } else {
                this.showToast(result.message || '删除失败', 'error');
            }
        } catch (error) {
            this.showToast('删除失败', 'error');
        }
    },
    
    async previewFile(id) {
        try {
            const response = await knowledgeFetchWithTimeout(`/api/rag/files/${id}/content`);
            const result = await response.json();
            
            if (result.success || result.content) {
                const content = result.content || '';
                const previewHtml = `
                    <div class="file-preview-content">
                        <pre style="white-space: pre-wrap; word-wrap: break-word; max-height: 400px; overflow-y: auto; background: #f8f9fa; padding: 15px; border-radius: 8px;">${this.escapeHtml(content.substring(0, 5000))}${content.length > 5000 ? '\n\n... (内容过长，已截断)' : ''}</pre>
                    </div>
                `;
                
                const previewContentEl = document.getElementById('file-preview-content');
                const previewModalEl = document.getElementById('filePreviewModal');
                
                if (previewContentEl) previewContentEl.innerHTML = previewHtml;
                if (previewModalEl) {
                    const modal = new bootstrap.Modal(previewModalEl);
                    modal.show();
                }
            } else {
                this.showToast(result.message || '无法获取文件内容', 'error');
            }
        } catch (error) {
            this.showToast('预览失败: ' + error.message, 'error');
        }
    },
    
    async processFile(id, enterpriseId = 'default') {
        this.showToast('正在处理文件...', 'info');
        
        try {
            const params = enterpriseId ? `?enterprise_id=${encodeURIComponent(enterpriseId)}` : '';
            const response = await knowledgeFetchWithTimeout(`/api/enterprise/files/${id}/process${params}`, { method: 'POST' });
            const result = await response.json();
            
            if (result.success || result.code === 0) {
                this.showToast('文件处理完成', 'success');
                await Promise.all([
                    this.loadUploadedFiles(),
                    this.loadKnowledge(),
                    this.refreshTopStatistics()
                ]);
            } else {
                this.showToast(result.message || '处理失败', 'error');
            }
        } catch (error) {
            this.showToast('处理失败', 'error');
        }
    },
    
    toggleKnowledgeSelect(id) {
        if (this.state.selectedKnowledge.has(id)) {
            this.state.selectedKnowledge.delete(id);
        } else {
            this.state.selectedKnowledge.add(id);
        }
        this.syncKnowledgeSelectionState();
    },
    
    toggleKnowledgeSelectAll() {
        const selectAllEl = document.getElementById('knowledge-select-all');
        const selectAll = selectAllEl ? selectAllEl.checked : false;
        
        document.querySelectorAll('.knowledge-checkbox').forEach(cb => {
            cb.checked = selectAll;
            const id = cb.dataset.id;
            if (selectAll) {
                this.state.selectedKnowledge.add(id);
            } else {
                this.state.selectedKnowledge.delete(id);
            }
        });
        this.syncKnowledgeSelectionState();
    },

    syncKnowledgeSelectionState() {
        const currentIds = this.state.knowledge.map(item => String(item.id || ''));
        const currentCheckboxes = Array.from(document.querySelectorAll('.knowledge-checkbox'));
        const checkedCount = currentIds.filter(id => this.state.selectedKnowledge.has(id)).length;
        const totalCount = currentIds.length;
        const selectAllEl = document.getElementById('knowledge-select-all');
        if (selectAllEl) {
            selectAllEl.checked = totalCount > 0 && checkedCount === totalCount;
            selectAllEl.indeterminate = checkedCount > 0 && checkedCount < totalCount;
        }

        currentCheckboxes.forEach(cb => {
            const id = String(cb.dataset.id || '');
            cb.checked = this.state.selectedKnowledge.has(id);
        });

        const selectedCountEl = document.getElementById('knowledge-selected-count');
        if (selectedCountEl) {
            selectedCountEl.textContent = this.state.selectedKnowledge.size;
        }

        const batchDeleteBtn = document.getElementById('knowledge-batch-delete-btn');
        if (batchDeleteBtn) {
            batchDeleteBtn.disabled = this.state.selectedKnowledge.size === 0;
        }
    },
    
    async batchKnowledgeAction(action) {
        if (this.state.selectedKnowledge.size === 0) {
            this.showToast('请先选择知识条目', 'warning');
            return;
        }

        if (action !== 'delete') {
            this.showToast('知识库页面已移除启用/禁用功能，只保留批量删除', 'warning');
            return;
        }

        if (!confirm(`确定要删除选中的 ${this.state.selectedKnowledge.size} 条知识吗？`)) {
            return;
        }
        
        try {
            const response = await knowledgeFetchWithTimeout('/api/knowledge/batch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    item_ids: Array.from(this.state.selectedKnowledge),
                    action: action
                })
            });
            
            const result = await response.json();
            
            if (result.success || result.code === 0) {
                this.showToast(result.message || '批量操作成功', 'success');
                this.state.selectedKnowledge.clear();
                await Promise.all([
                    this.loadKnowledge(),
                    this.refreshTopStatistics()
                ]);
            } else {
                this.showToast(result.message || '操作失败', 'error');
            }
        } catch (error) {
            this.showToast('操作失败: ' + error.message, 'error');
        }
    },
    
    changeKnowledgePage(direction) {
        if (direction === 'prev' && this.state.currentPage > 1) {
            this.state.currentPage--;
        } else if (direction === 'next' && this.state.currentPage < this.state.totalPages) {
            this.state.currentPage++;
        }
        this.loadKnowledge();
    },
    
    goToKnowledgePage(page) {
        this.state.currentPage = page;
        this.loadKnowledge();
    },
    
    changeKnowledgePageSize() {
        const pageSizeEl = document.getElementById('knowledge-page-size');
        this.state.pageSize = pageSizeEl ? parseInt(pageSizeEl.value) : 50;
        this.state.currentPage = 1;
        this.loadKnowledge();
    },
    
    async exportKnowledge() {
        try {
            const response = await knowledgeFetchWithTimeout('/api/knowledge/export');
            const blob = await response.blob();
            const disposition = response.headers.get('Content-Disposition') || '';
            const filenameMatch = disposition.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/i);
            const rawFilename = filenameMatch ? (filenameMatch[1] || filenameMatch[2] || '') : '';
            const filename = rawFilename ? decodeURIComponent(rawFilename) : `knowledge_export_${Date.now()}.json`;

            const downloadUrl = window.URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = downloadUrl;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            link.remove();
            window.URL.revokeObjectURL(downloadUrl);
        } catch (error) {
            this.showToast('导出失败: ' + error.message, 'error');
        }
    },
    
    importKnowledge() {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = '.json,.csv,.txt';
        input.onchange = async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            
            const formData = new FormData();
            formData.append('file', file);
            
            try {
                const response = await knowledgeFetchWithTimeout('/api/knowledge/upload', {
                    method: 'POST',
                    body: formData
                });
                
                const result = await response.json();
                
                if (result.success) {
                    this.showToast(result.message || `导入成功: ${result.count || 0} 条`, 'success');
                    await Promise.all([
                        this.loadKnowledge(),
                        this.refreshTopStatistics()
                    ]);
                } else {
                    this.showToast(result.message || '导入失败', 'error');
                }
            } catch (error) {
                this.showToast('导入失败', 'error');
            }
        };
        input.click();
    },
    
    showGraphRAG() {
        const modalEl = document.getElementById('graphModal');
        if (modalEl) {
            const modal = new bootstrap.Modal(modalEl);
            modal.show();
        }
        this.refreshGraph();
    },
    
    async refreshGraph() {
        const container = document.getElementById('graph-container');
        if (container) container.innerHTML = '<div class="text-center p-4"><div class="spinner-border text-primary" role="status"></div><br><small class="text-muted">加载中...</small></div>';
        
        try {
            const domainEl = document.getElementById('graph-domain-filter');
            const searchEl = document.getElementById('graph-search');
            const domain = domainEl ? domainEl.value : '';
            const search = searchEl ? searchEl.value : '';
            
            const params = new URLSearchParams();
            if (domain) params.append('domain', domain);
            if (search) params.append('query', search);
            
            const response = await knowledgeFetchWithTimeout('/api/graph/data?' + params);
            const result = await response.json();
            
            if (result.entities || result.data) {
                await this.renderGraph(result);
            } else {
                if (container) container.innerHTML = '<div class="text-center text-muted p-4">暂无图谱数据</div>';
            }
        } catch (error) {
            if (container) { container.textContent = ''; const div = document.createElement('div'); div.className = 'text-center text-danger p-4'; div.textContent = '加载失败: ' + error.message; container.appendChild(div); }
        }
    },

    /**
     * 按需加载 ECharts，避免首页全局注入图表脚本。
     * @returns {Promise<object>} 已加载的 ECharts 全局对象
     */
    async ensureEchartsLoaded() {
        if (typeof window.echarts !== 'undefined') {
            return window.echarts;
        }

        if (!this._echartsLoaderPromise) {
            this._echartsLoaderPromise = new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = '/static/echarts.min.js';
                script.async = true;
                script.onload = () => {
                    if (typeof window.echarts !== 'undefined') {
                        resolve(window.echarts);
                        return;
                    }
                    reject(new Error('ECharts loaded but window.echarts is unavailable'));
                };
                script.onerror = () => reject(new Error('Failed to load ECharts'));
                document.head.appendChild(script);
            }).catch((error) => {
                this._echartsLoaderPromise = null;
                throw error;
            });
        }

        return this._echartsLoaderPromise;
    },

    /**
     * 渲染知识图谱。
     * @param {object} data - 图谱数据
     * @returns {Promise<void>}
     */
    async renderGraph(data) {
        const container = document.getElementById('graph-container');
        container.innerHTML = '<div id="graph-canvas" style="width: 100%; height: 400px;"></div>';
        
        const entities = data.entities || (data.data && data.data.entities) || [];
        const relations = data.relations || (data.data && data.data.relations) || [];
        
        if (entities.length === 0) {
            container.innerHTML = '<div class="text-center text-muted p-4">暂无图谱数据，请先上传文档或添加知识</div>';
            return;
        }
        
        const echartsLib = await this.ensureEchartsLoaded();
        if (typeof echartsLib !== 'undefined') {
            const chart = echartsLib.init(document.getElementById('graph-canvas'));
            
            const categoryColors = {
                'attractions': '#5470c6',
                'food': '#91cc75',
                'transport': '#fac858',
                'accommodation': '#ee6666',
                'itinerary': '#73c0de',
                'tips': '#3ba272',
                'price': '#fc8452',
                'domain': '#9a60b4',
                'topic': '#ea7ccc',
                'default': '#48b8d0'
            };
            
            const nodes = entities.map(e => {
                const props = e.properties || {};
                const category = e.entityType || e.type || 'default';
                return {
                    id: e.id,
                    name: e.name || props.name || e.id,
                    category: category,
                    symbolSize: Math.max(20, Math.min(50, 30 + (e.relationCount || 0) * 5)),
                    itemStyle: {
                        color: categoryColors[category] || categoryColors['default']
                    }
                };
            });
            
            const links = relations.map(r => ({
                source: r.source_id || r.sourceId || r.source,
                target: r.target_id || r.targetId || r.target,
                value: r.relation || r.relationType || '关联',
                lineStyle: {
                    width: 1,
                    curveness: 0.1
                }
            }));
            
            const categories = [...new Set(nodes.map(n => n.category))].map(cat => ({
                name: cat,
                itemStyle: { color: categoryColors[cat] || categoryColors['default'] }
            }));
            
            chart.setOption({
                tooltip: {
                    formatter: function(params) {
                        if (params.dataType === 'node') {
                            return `<strong>${KnowledgeApp.escapeHtml(params.data.name)}</strong><br>类型: ${KnowledgeApp.escapeHtml(params.data.category)}`;
                        } else {
                            return `${KnowledgeApp.escapeHtml(params.data.source)} → ${KnowledgeApp.escapeHtml(params.data.target)}<br>关系: ${KnowledgeApp.escapeHtml(String(params.data.value))}`;
                        }
                    }
                },
                legend: {
                    data: categories.map(c => c.name),
                    orient: 'vertical',
                    right: 10,
                    top: 10
                },
                series: [{
                    type: 'graph',
                    layout: 'force',
                    data: nodes,
                    links: links,
                    categories: categories,
                    roam: true,
                    draggable: true,
                    label: {
                        show: true,
                        position: 'right',
                        fontSize: 10
                    },
                    force: {
                        repulsion: 200,
                        edgeLength: [50, 150],
                        gravity: 0.1
                    },
                    emphasis: {
                        focus: 'adjacency',
                        lineStyle: {
                            width: 3
                        }
                    }
                }]
            });
            
            chart.on('click', function(params) {
                if (params.dataType === 'node') {
                    document.getElementById('entity-detail').innerHTML = `
                        <strong>${KnowledgeApp.escapeHtml(params.data.name)}</strong><br>
                        <small class="text-muted">ID: ${KnowledgeApp.escapeHtml(String(params.data.id))}</small><br>
                        <small class="text-muted">类型: ${KnowledgeApp.escapeHtml(params.data.category)}</small>
                    `;
                }
            });
            
            if (this._graphResizeHandler) {
                window.removeEventListener('resize', this._graphResizeHandler);
            }
            this._graphResizeHandler = () => chart.resize();
            window.addEventListener('resize', this._graphResizeHandler);
        } else {
            container.innerHTML = '<div class="text-center text-warning p-4">ECharts未加载，无法显示图谱</div>';
        }
    },
    
    loadTheme() {
        const saved = localStorage.getItem('kb-theme');
        if (saved === 'dark') {
            document.documentElement.setAttribute('data-theme', 'dark');
            this.state.theme = 'dark';
        }
    },
    
    toggleTheme() {
        this.state.theme = this.state.theme === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', this.state.theme);
        localStorage.setItem('kb-theme', this.state.theme);
    },
    
    showHelp() {
        alert('智揽人工智能模型 v1.0.0\n\n功能说明:\n- 知识资产: 管理知识条目、领域、主题与元数据\n- 学习结果: 在知识库页直接审核待学习知识并查看已学习规模\n- 行业 Schema: 维护字段结构、意图关键词与 grounded 配置\n\n快捷键:\n- Ctrl+N: 新建知识\n- Ctrl+F: 搜索知识');
    },
    
    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast show align-items-center text-white bg-${type === 'error' ? 'danger' : type === 'success' ? 'success' : type === 'warning' ? 'warning' : 'primary'}`;
        const toastBody = document.createElement('div');
        toastBody.className = 'd-flex';
        const bodyText = document.createElement('div');
        bodyText.className = 'toast-body';
        bodyText.textContent = message;
        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'btn-close btn-close-white me-2 m-auto';
        closeBtn.onclick = () => toast.remove();
        toastBody.appendChild(bodyText);
        toastBody.appendChild(closeBtn);
        toast.appendChild(toastBody);
        container.appendChild(toast);
        
        setTimeout(() => toast.remove(), 3000);
    },
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },
    
    formatFileSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    },
    
    formatDate(dateStr) {
        if (!dateStr) return '-';
        const date = new Date(dateStr);
        return date.toLocaleDateString('zh-CN') + ' ' + date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    },

    formatRelativeDate(dateStr) {
        if (!dateStr) return '-';
        const date = new Date(dateStr);
        if (Number.isNaN(date.getTime())) return '-';
        const now = new Date();
        const diffMs = now - date;
        const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
        if (diffHours < 1) return '刚刚';
        if (diffHours < 24) return `${diffHours} 小时前`;
        const diffDays = Math.floor(diffHours / 24);
        if (diffDays < 30) return `${diffDays} 天前`;
        return this.formatDate(dateStr);
    }
};

document.addEventListener('DOMContentLoaded', () => {
    KnowledgeApp.init();
});

// Keep a minimal set of global entry points for existing template callbacks
// and a few cross-module integrations (for example `learning.js`).
const knowledgeGlobalExports = {
    KnowledgeApp,
    loadKnowledge: (...args) => KnowledgeApp.loadKnowledge(...args),
    showAddKnowledgeModal: (...args) => KnowledgeApp.showAddKnowledgeModal(...args),
    saveKnowledge: (...args) => KnowledgeApp.saveKnowledge(...args),
    openKnowledgeById: (...args) => KnowledgeApp.openKnowledgeById(...args),
    toggleKnowledgeSelectAll: (...args) => KnowledgeApp.toggleKnowledgeSelectAll(...args),
    batchKnowledgeAction: (...args) => KnowledgeApp.batchKnowledgeAction(...args),
    changeKnowledgePage: (...args) => KnowledgeApp.changeKnowledgePage(...args),
    goToKnowledgePage: (...args) => KnowledgeApp.goToKnowledgePage(...args),
    changeKnowledgePageSize: (...args) => KnowledgeApp.changeKnowledgePageSize(...args),
    loadUploadedFiles: (...args) => KnowledgeApp.loadUploadedFiles(...args),
    switchKnowledgeWorkspace: (...args) => KnowledgeApp.switchWorkspace(...args),
    onKnowledgePanelActivated: (...args) => KnowledgeApp.onPanelActivated(...args),
};

Object.assign(window, knowledgeGlobalExports);
