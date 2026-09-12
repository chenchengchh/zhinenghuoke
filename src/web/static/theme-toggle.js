/**
 * 暗黑模式切换器
 * 使用示例:
 * ThemeToggle.init();
 * ThemeToggle.toggle();
 * ThemeToggle.setTheme('dark');
 */

class ThemeToggle {
  static STORAGE_KEY = 'theme';
  static DARK_CLASS = 'dark-mode';
  static _initialized = false;

  /**
   * 初始化主题切换器
   */
  static init() {
    if (this._initialized) return;
    this._initialized = true;

    const savedTheme = localStorage.getItem(this.STORAGE_KEY);
    if (savedTheme) {
      this.applyTheme(savedTheme);
    } else {
      this.applySystemPreference();
    }

    this.ensureToggleButton();
    this.bindEvents();
  }

  /**
   * 确保切换按钮存在
   */
  static ensureToggleButton() {
    let button = document.querySelector('.theme-toggle');
    if (button) return;

    button = document.createElement('button');
    button.className = 'theme-toggle';
    button.setAttribute('type', 'button');
    button.setAttribute('aria-label', '切换主题');
    button.setAttribute('title', '切换暗黑模式');
    button.innerHTML = `
      <span class="icon-moon" aria-hidden="true">🌙</span>
      <span class="icon-sun" aria-hidden="true">☀️</span>
    `;

    document.body.appendChild(button);
  }

  /**
   * 绑定事件（仅绑定一次）
   */
  static bindEvents() {
    const button = document.querySelector('.theme-toggle');
    if (button && !button._themeBound) {
      button._themeBound = true;
      button.addEventListener('click', () => this.toggle());
    }

    if (!this._systemListenerAdded) {
      this._systemListenerAdded = true;
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
        if (!localStorage.getItem(this.STORAGE_KEY)) {
          this.setTheme(e.matches ? 'dark' : 'light');
        }
      });
    }
  }

  /**
   * 切换主题
   */
  static toggle() {
    const isDark = document.body.classList.contains(this.DARK_CLASS);
    const newTheme = isDark ? 'light' : 'dark';
    this.setTheme(newTheme);
  }

  /**
   * 设置主题
   * @param {string} theme - 'dark' 或 'light'
   */
  static setTheme(theme) {
    this.applyTheme(theme);
    localStorage.setItem(this.STORAGE_KEY, theme);
    this.updateToggleButton(theme);
  }

  /**
   * 应用主题到DOM
   * @param {string} theme - 'dark' 或 'light'
   */
  static applyTheme(theme) {
    if (theme === 'dark') {
      document.body.classList.add(this.DARK_CLASS);
    } else {
      document.body.classList.remove(this.DARK_CLASS);
    }
  }

  /**
   * 获取当前主题
   * @returns {string} 'dark' 或 'light'
   */
  static getTheme() {
    return document.body.classList.contains(this.DARK_CLASS) ? 'dark' : 'light';
  }

  /**
   * 应用系统偏好
   */
  static applySystemPreference() {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    this.applyTheme(prefersDark ? 'dark' : 'light');
  }

  /**
   * 更新切换按钮状态
   */
  static updateToggleButton(theme) {
    const button = document.querySelector('.theme-toggle');
    if (button) {
      button.setAttribute('aria-label', theme === 'dark' ? '切换到浅色模式' : '切换到深色模式');
    }
  }
}

window.ThemeToggle = ThemeToggle;
