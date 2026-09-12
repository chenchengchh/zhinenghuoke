/**
 * 确认对话框工具类
 * 使用示例:
 * ConfirmDialog.show('确定要删除吗？', {
 *   onConfirm: () => deleteItem(),
 *   onCancel: () => console.log('已取消')
 * });
 */

class ConfirmDialog {
  /**
   * 显示确认对话框
   * @param {string} message - 对话框消息
   * @param {Object} options - 配置选项
   */
  static show(message, options = {}) {
    const {
      title = '确认',
      confirmText = '确定',
      cancelText = '取消',
      confirmClass = 'btn-primary',
      onConfirm = null,
      onCancel = null,
      dangerMode = false
    } = options;

    const modalId = 'confirm-dialog-' + Date.now();

    const modalHtml = `
      <div class="modal fade" id="${modalId}" tabindex="-1">
        <div class="modal-dialog modal-dialog-centered">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title">${this.escapeHtml(title)}</h5>
              <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
              <p>${this.escapeHtml(message)}</p>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">${this.escapeHtml(cancelText)}</button>
              <button type="button" class="btn ${dangerMode ? 'btn-danger' : confirmClass}" id="${modalId}-confirm">${this.escapeHtml(confirmText)}</button>
            </div>
          </div>
        </div>
      </div>
    `;

    const container = document.createElement('div');
    container.innerHTML = modalHtml;
    const modalElement = container.firstElementChild;

    document.body.appendChild(modalElement);

    const confirmButton = document.getElementById(`${modalId}-confirm`);
    confirmButton.addEventListener('click', () => {
      if (onConfirm) onConfirm();
      bootstrap.Modal.getInstance(modalElement)?.hide();
    });

    const modal = new bootstrap.Modal(modalElement);

    modalElement.addEventListener('hidden.bs.modal', () => {
      if (onCancel) onCancel();
      modalElement.remove();
    });

    modal.show();

    return modal;
  }

  /**
   * 显示危险操作确认对话框
   * @param {string} message - 对话框消息
   * @param {Function} onConfirm - 确认回调
   */
  static danger(message, onConfirm) {
    return this.show(message, {
      title: '危险操作',
      confirmText: '确认删除',
      dangerMode: true,
      onConfirm
    });
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

window.ConfirmDialog = ConfirmDialog;
