/**
 * 骨架屏工具类
 * 使用示例:
 * Skeleton.show('#container');
 * Skeleton.hide('#container');
 * Skeleton.toggle('#container', isLoading);
 */

class Skeleton {
  /**
   * 显示骨架屏
   * @param {string} selector - 容器选择器
   * @param {Object} options - 配置选项
   */
  static show(selector, options = {}) {
    const container = document.querySelector(selector);
    if (!container) {
      console.warn(`Skeleton: 未找到容器 ${selector}`);
      return;
    }

    const {
      count = 3,
      type = 'list',
      overlay = false
    } = options;

    const skeleton = document.createElement('div');
    skeleton.className = 'skeleton-container';
    skeleton.dataset.skeleton = 'true';

    if (overlay) {
      skeleton.classList.add('skeleton-overlay');
    }

    switch (type) {
      case 'list':
        skeleton.innerHTML = this.generateListSkeleton(count);
        break;
      case 'card':
        skeleton.innerHTML = this.generateCardSkeleton(count);
        break;
      case 'table':
        skeleton.innerHTML = this.generateTableSkeleton(count);
        break;
      case 'chat':
        skeleton.innerHTML = this.generateChatSkeleton(count);
        break;
      default:
        skeleton.innerHTML = this.generateListSkeleton(count);
    }

    container.appendChild(skeleton);
    return skeleton;
  }

  /**
   * 隐藏骨架屏
   * @param {string} selector - 容器选择器
   */
  static hide(selector) {
    const container = document.querySelector(selector);
    if (!container) return;

    const skeleton = container.querySelector('[data-skeleton="true"]');
    if (skeleton) {
      skeleton.remove();
    }
  }

  /**
   * 切换骨架屏状态
   * @param {string} selector - 容器选择器
   * @param {boolean} isLoading - 是否显示骨架屏
   * @param {Object} options - 配置选项
   */
  static toggle(selector, isLoading, options = {}) {
    if (isLoading) {
      this.show(selector, options);
    } else {
      this.hide(selector);
    }
  }

  /**
   * 生成列表骨架屏 HTML
   */
  static generateListSkeleton(count) {
    let html = '';
    for (let i = 0; i < count; i++) {
      html += `
        <div class="skeleton-list-item">
          <div class="skeleton skeleton-avatar"></div>
          <div class="flex-grow-1">
            <div class="skeleton skeleton-text" style="width: 40%;"></div>
            <div class="skeleton skeleton-text" style="width: 70%;"></div>
          </div>
        </div>
      `;
    }
    return html;
  }

  /**
   * 生成卡片骨架屏 HTML
   */
  static generateCardSkeleton(count) {
    let html = '';
    for (let i = 0; i < count; i++) {
      html += `
        <div class="skeleton-card" style="margin-bottom: var(--spacing-4);">
          <div class="skeleton skeleton-title"></div>
          <div class="skeleton skeleton-text"></div>
          <div class="skeleton skeleton-text"></div>
          <div class="skeleton skeleton-text" style="width: 60%;"></div>
        </div>
      `;
    }
    return html;
  }

  /**
   * 生成表格骨架屏 HTML
   */
  static generateTableSkeleton(count) {
    const rows = [];
    for (let i = 0; i < count; i++) {
      rows.push(`
        <tr>
          <td><div class="skeleton skeleton-text" style="width: 20px;"></div></td>
          <td><div class="skeleton skeleton-text" style="width: 100px;"></div></td>
          <td><div class="skeleton skeleton-text" style="width: 150px;"></div></td>
          <td><div class="skeleton skeleton-text" style="width: 80px;"></div></td>
        </tr>
      `);
    }
    return `
      <table class="table">
        <thead>
          <tr>
            <th>#</th>
            <th>名称</th>
            <th>描述</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          ${rows.join('')}
        </tbody>
      </table>
    `;
  }

  /**
   * 生成聊天骨架屏 HTML
   */
  static generateChatSkeleton(count) {
    let html = '';
    for (let i = 0; i < count; i++) {
      const isInbound = i % 2 === 0;
      html += `
        <div class="message-item ${isInbound ? 'message-inbound' : 'message-outbound'}" style="animation: none; opacity: 1;">
          <div class="skeleton skeleton-avatar"></div>
          <div class="flex-grow-1">
            <div class="skeleton skeleton-text" style="width: 60%;"></div>
            <div class="skeleton skeleton-text"></div>
            <div class="skeleton skeleton-text" style="width: 40%;"></div>
          </div>
        </div>
      `;
    }
    return html;
  }

  /**
   * 显示加载spinner
   * @param {string} selector - 容器选择器
   * @param {string} message - 加载提示文本
   */
  static showSpinner(selector, message = '加载中...') {
    const container = document.querySelector(selector);
    if (!container) return;

    const spinner = document.createElement('div');
    spinner.className = 'skeleton-overlay';
    spinner.dataset.skeleton = 'spinner';
    spinner.innerHTML = `
      <div class="text-center">
        <div class="spinner-border text-primary mb-3" role="status">
          <span class="visually-hidden">加载中...</span>
        </div>
        <div class="text-muted">${this.escapeHtml(message)}</div>
      </div>
    `;

    container.appendChild(spinner);
    return spinner;
  }

  /**
   * XSS防护
   */
  static escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

window.Skeleton = Skeleton;
