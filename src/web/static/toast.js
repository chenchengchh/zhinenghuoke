/**
 * Toast 通知工具类
 * 
 * 使用示例:
 * Toast.show('操作成功', 'success');
 * Toast.show('操作失败', 'error');
 * Toast.show('警告信息', 'warning');
 * Toast.show('提示信息', 'info');
 * 
 * 自定义选项:
 * Toast.show('标题', 'success', {
 *   message: '详细内容',
 *   duration: 5000,
 *   onClose: () => console.log('Toast 已关闭')
 * });
 */

class Toast {
  /**
   * 显示 Toast 通知
   * @param {string} title - 标题
   * @param {string} type - 类型：success, error, warning, info
   * @param {object} options - 选项
   * @param {string} options.message - 详细消息（可选）
   * @param {number} options.duration - 持续时间（毫秒），0 表示不自动关闭
   * @param {function} options.onClose - 关闭回调
   */
  static show(title, type = 'info', options = {}) {
    const {
      message = '',
      duration = 3000,
      onClose = null
    } = options;

    // 创建或获取容器
    let container = document.querySelector('.toast-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container';
      document.body.appendChild(container);
    }

    // 创建 Toast 元素
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    
    const icon = this.getIcon(type);
    
    toast.innerHTML = `
      <div class="toast-icon">${icon}</div>
      <div class="toast-content">
        <div class="toast-title">${this.escapeHtml(title)}</div>
        ${message ? `<div class="toast-message">${this.escapeHtml(message)}</div>` : ''}
      </div>
      <button class="toast-close" aria-label="关闭">&times;</button>
    `;

    // 添加到容器
    container.appendChild(toast);

    // 关闭按钮事件
    const closeBtn = toast.querySelector('.toast-close');
    const closeToast = () => {
      toast.classList.add('hiding');
      setTimeout(() => {
        toast.remove();
        if (onClose) onClose();
        
        // 如果容器为空，移除容器
        if (container.children.length === 0) {
          container.remove();
        }
      }, 200); // 等待动画完成
    };

    closeBtn.addEventListener('click', closeToast);

    // 自动关闭
    if (duration > 0) {
      setTimeout(closeToast, duration);
    }

    return {
      close: closeToast,
      element: toast
    };
  }

  /**
   * 显示成功 Toast
   * @param {string} title - 标题
   * @param {object} options - 选项
   */
  static success(title, options = {}) {
    return this.show(title, 'success', options);
  }

  /**
   * 显示错误 Toast
   * @param {string} title - 标题
   * @param {object} options - 选项
   */
  static error(title, options = {}) {
    return this.show(title, 'error', options);
  }

  /**
   * 显示警告 Toast
   * @param {string} title - 标题
   * @param {object} options - 选项
   */
  static warning(title, options = {}) {
    return this.show(title, 'warning', options);
  }

  /**
   * 显示信息 Toast
   * @param {string} title - 标题
   * @param {object} options - 选项
   */
  static info(title, options = {}) {
    return this.show(title, 'info', options);
  }

  /**
   * 获取对应类型的图标
   * @param {string} type - 类型
   * @returns {string} 图标 emoji
   */
  static getIcon(type) {
    const icons = {
      success: '✓',
      error: '✕',
      warning: '⚠',
      info: 'ℹ'
    };
    return icons[type] || icons.info;
  }

  /**
   * HTML 转义，防止 XSS
   * @param {string} text - 文本
   * @returns {string} 转义后的文本
   */
  static escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

// 导出为全局变量
window.Toast = Toast;
