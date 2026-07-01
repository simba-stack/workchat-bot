// ═══════════════════════════════════════════════════════════
// PRIDE OPERATIONS · Frontend app (Sprint 1)
// Alpine.data() register + TG Login Widget + theme toggle
// ═══════════════════════════════════════════════════════════

// ─── Theme toggle (загружается сразу, до Alpine) ─────────
function initTheme() {
    const stored = localStorage.getItem('pride-theme');
    const preferred = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    const theme = stored || preferred;
    document.documentElement.setAttribute('data-theme', theme);
    updateThemeIcon(theme);
}
function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('pride-theme', next);
    updateThemeIcon(next);
}
function updateThemeIcon(theme) {
    const icon = document.getElementById('theme-icon');
    if (icon) icon.textContent = theme === 'dark' ? '🌙' : '☀️';
}
initTheme();
window.toggleTheme = toggleTheme;

// ─── Alpine.data register — ГАРАНТИРОВАННЫЙ способ v3.x ──
document.addEventListener('alpine:init', () => {
    Alpine.data('app', () => ({
        user: null,
        status: 'Инициализация…',
        botUsername: null,

        async init() {
            this.status = 'Проверка сессии…';
            await this.checkAuth();
            if (!this.user) {
                await this.loadConfig();
                this.setupTelegramWidget();
                this.status = 'Готов к авторизации';
            }
        },

        async checkAuth() {
            try {
                const r = await fetch('/api/me', { credentials: 'include' });
                if (r.ok) {
                    const data = await r.json();
                    this.user = data.user;
                    this.status = 'Сессия активна';
                } else if (r.status === 401) {
                    const refreshed = await this.tryRefresh();
                    if (refreshed) return this.checkAuth();
                }
            } catch (e) {
                console.error('Auth check failed:', e);
                this.status = 'Ошибка проверки сессии';
            }
        },

        async tryRefresh() {
            try {
                const r = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' });
                return r.ok;
            } catch { return false; }
        },

        async loadConfig() {
            try {
                const r = await fetch('/api/config');
                if (r.ok) {
                    const data = await r.json();
                    this.botUsername = data.bot_username;
                }
            } catch (e) {
                console.warn('Config load failed:', e);
                this.botUsername = 'PrideInviteWork_bot';
            }
        },

        setupTelegramWidget() {
            const container = document.getElementById('tg-login-widget');
            if (!container || !this.botUsername) return;
            window.onTelegramAuth = async (tgUser) => {
                this.status = 'Авторизация…';
                try {
                    const r = await fetch('/api/auth/telegram', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        credentials: 'include',
                        body: JSON.stringify(tgUser),
                    });
                    if (r.ok) {
                        const data = await r.json();
                        this.user = data.user;
                        this.status = 'Успех!';
                    } else {
                        const err = await r.json();
                        this.status = `Ошибка: ${err.detail || r.statusText}`;
                    }
                } catch (e) {
                    this.status = `Сетевая ошибка: ${e.message}`;
                }
            };
            const script = document.createElement('script');
            script.async = true;
            script.src = 'https://telegram.org/js/telegram-widget.js?22';
            script.setAttribute('data-telegram-login', this.botUsername);
            script.setAttribute('data-size', 'large');
            script.setAttribute('data-radius', '12');
            script.setAttribute('data-onauth', 'onTelegramAuth(user)');
            script.setAttribute('data-request-access', 'write');
            container.innerHTML = '';
            container.appendChild(script);
        },

        async logout() {
            try {
                await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
            } finally {
                this.user = null;
                this.status = 'Вы вышли';
                setTimeout(() => this.setupTelegramWidget(), 100);
            }
        },
    }));
});
