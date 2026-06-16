# Telegram Mini App — knowledge для PRIDE P2P
*Собрано: 2026-06-16. Источники: core.telegram.org/bots/webapps, docs.telegram-mini-apps.com, Bot API changelog 7.0→10.1, Wallet @wallet / @CryptoBot UX patterns.*

> Этот файл — справочник для будущих сессий Claude при работе над `pride-outkup-service/miniapp/`. Читать при старте задач по UI/UX/API.

---

## Bot API версии (доступно сейчас в реальных клиентах)

| Версия | Главное |
|---|---|
| 6.0 | WebApp базовый, MainButton, BackButton |
| 6.1 | HapticFeedback, setHeaderColor, openInvoice |
| 6.2 | showPopup/showAlert/showConfirm, enableClosingConfirmation |
| 6.4 | showScanQrPopup, readTextFromClipboard |
| 6.9 | CloudStorage (1024 ключей, 4096-байт значение), requestContact, requestWriteAccess |
| 7.0 | SettingsButton, расширены ThemeParams (section_bg_color, accent_text_color, destructive_text_color) |
| 7.2 | **BiometricManager** (Face ID/Touch ID) — мы уже используем |
| 7.4 | **Stars (XTR currency)**, refundStarPayment |
| 7.7 | **disableVerticalSwipes** — критично для wallet UX |
| 7.8 | shareToStory |
| 7.10 | SecondaryButton, setBottomBarColor |
| 8.0 | requestFullscreen, safeAreaInset, **addToHomeScreen**, shareMessage, downloadFile, activated/deactivated, photo_url юзера |
| 9.0 | **DeviceStorage (5 МБ)** + **SecureStorage (Keychain/Keystore)** — для KYC tokens |
| 9.1 | hideKeyboard() |
| 9.4 | цветные кнопки в bot (icon_custom_emoji_id + style) — мы используем в @PrideOutsource_bot |
| 9.6 | requestChat() + savePreparedKeyboardButton |
| 10.0 | Guest Mode |
| 10.1 | RichMessage (streaming AI) |

---

## Init data verification (на сервере, ОБЯЗАТЕЛЬНО)

```python
import hmac, hashlib, time
from urllib.parse import parse_qsl

def verify_init_data(raw: str, bot_token: str, max_age=300) -> dict:
    parsed = dict(parse_qsl(raw, strict_parsing=True))
    received = parsed.pop("hash")
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise ValueError("bad signature")
    if time.time() - int(parsed["auth_date"]) > max_age:
        raise ValueError("init data expired")
    return parsed
```

**Header convention**: `Authorization: tma <raw-init-data>`.

**Ошибки которых избегать**:
- НЕ hex-кодировать `secret_key` между двумя HMACs (он raw bytes!)
- НЕ url-decode значения перед `sorted` — пары сортируются как сырые строки
- `==` вместо `hmac.compare_digest` — timing attack
- max_age=3600 для withdraw — нужно ≤300с + per-request nonce
- Читать `initDataUnsafe` на сервере — спуфится в DevTools

---

## Launch params (URL hash)

`tgWebAppData`, `tgWebAppVersion`, `tgWebAppPlatform` (android/ios/tdesktop/macos/weba/webk), `tgWebAppThemeParams`, `tgWebAppStartParam` (≤64 байт, deeplink), `tgWebAppFullscreen`, `tgWebAppBotInline`.

**Критично**: на refresh hash теряется → кешировать в sessionStorage в ПЕРВОЙ строке JS, до любого роутера:

```js
let h = sessionStorage.tgLP || window.location.hash.slice(1);
if (h && !sessionStorage.tgLP) sessionStorage.tgLP = h;
const lp = new URLSearchParams(h || '');
```

---

## TOP-10: что добавить в PRIDE P2P (низкоусильно, высокоимпактно)

1. **`Telegram.WebApp.disableVerticalSwipes()`** на init — одна строка, останавливает случайный close при скролле страницы вниз. Bot API 7.7+.
2. **CSS View Transitions API** для tab switcher (Wallet / P2P / История / Ещё): `document.startViewTransition(() => render())`. Браузер сам делает crossfade. Поддержка >85% в WebView 2026.
3. **`enableClosingConfirmation()`** на время withdraw/swap (показать "Discard changes?"), отключать на success/cancel.
4. **`SecureStorage`** (9.0) для KYC token + biometric flag вместо `localStorage`. Encrypted Keychain/Keystore.
5. **`hideKeyboard()`** (9.1) после `submit` форм.
6. **`activated`/`deactivated` events** — паузить polling/Chart.js когда приложение в фоне. Экономит Railway egress.
7. **`shareToStory()`** после успешной сделки — органик рефералка с deeplink `?startapp=ref_<userid>`.
8. **`addToHomeScreen()` + `checkHomeScreenStatus()`** на первой успешной сделке — PWA-like установка без PWA-боли.
9. **Inline SVG checkmark** вместо Lottie плеера. Экономит ~30 КБ:
   ```html
   <svg viewBox="0 0 52 52">
     <circle cx="26" cy="26" r="25" fill="none" stroke="#22c55e" stroke-width="2"/>
     <path d="M14 27l8 8 16-18" fill="none" stroke="#22c55e" stroke-width="3"
           stroke-dasharray="48" stroke-dashoffset="48">
       <animate attributeName="stroke-dashoffset" to="0" dur="0.4s" fill="freeze"/>
     </path>
   </svg>
   ```
10. **`SettingsButton.show()`** (7.0) — вынести Settings в нативное "..." меню Telegram.

---

## События `onEvent` (полный список)

`themeChanged`, `viewportChanged` ({isStateStable}), `mainButtonClicked`, `secondaryButtonClicked`, `backButtonClicked`, `settingsButtonClicked`, `invoiceClosed` ({url, status: paid|cancelled|failed|pending}), `popupClosed` ({button_id|null}), `qrTextReceived` ({data}), `scanQrPopupClosed`, `clipboardTextReceived`, `writeAccessRequested`, `contactRequested`, `biometricManagerUpdated`, `biometricAuthRequested` ({isAuthenticated, biometricToken?}), `biometricTokenUpdated`, `biometricAccessRequested`, `fullscreenChanged`, `fullscreenFailed` ({error: UNSUPPORTED|ALREADY_FULLSCREEN}), `activated`, `deactivated`, `safeAreaChanged`, `contentSafeAreaChanged`, `homeScreenAdded`, `homeScreenChecked`, `emojiStatusSet`, `shareMessageSent`, `fileDownloadRequested`, `locationManagerUpdated`, `accelerometer*`, `gyroscope*`, `deviceOrientation*`.

**Всегда `offEvent`** при teardown view (SPA).

---

## Theme: CSS variables

```css
:root {
  background: var(--tg-theme-bg-color);
  color: var(--tg-theme-text-color);
  padding-top: var(--tg-safe-area-inset-top);
  min-height: var(--tg-viewport-stable-height, 100vh);
}
```

Все params: `bg_color`, `text_color`, `hint_color`, `link_color`, `button_color`, `button_text_color`, `secondary_bg_color`, `header_bg_color`, `bottom_bar_bg_color`, `accent_text_color`, `section_bg_color`, `section_header_text_color`, `subtitle_text_color`, `destructive_text_color`, `section_separator_color`.

**Сбросить "white flash"** на dark theme — read theme synchronously в `<head>` ДО первого paint:

```html
<script>(function(){
  const p = new URLSearchParams(location.hash.slice(1));
  try {
    const t = JSON.parse(p.get('tgWebAppThemeParams') || '{}');
    const r = document.documentElement.style;
    for (const k in t) r.setProperty('--tg-' + k.replace(/_/g, '-'), t[k]);
  } catch {}
})();</script>
```

---

## BiometricManager — withdraw flow

```js
const bm = tg.BiometricManager;
bm.init(() => {
  if (!bm.isInited || !bm.isBiometricAvailable) return fallbackToPin();
  if (!bm.isAccessGranted) {
    bm.requestAccess({reason: 'Confirm crypto withdraw'}, granted => {
      if (granted) doAuth(); else tg.showAlert('Auth required');
    });
  } else doAuth();

  function doAuth() {
    bm.authenticate({reason: 'Sign withdraw'}, async (ok, token) => {
      if (!ok) return tg.HapticFeedback.notificationOccurred('error');
      await api.withdraw({
        nonce: crypto.randomUUID(),         // server-issued, prevents replay
        idempotency_key: crypto.randomUUID() // prevents double-spend
      });
      tg.HapticFeedback.notificationOccurred('success');
    });
  }
});
```

`biometricType === 'unknown'` на tdesktop — фолбэк. Token opaque — реальная проверка на сервере через свежесть initData.

---

## Storage strategy для PRIDE P2P

- **localStorage**: ephemeral UI state (текущий tab, открытые карточки). Wipes on iOS WKWebView в low storage condition.
- **sessionStorage**: launch params snapshot.
- **CloudStorage (6.9+)**: prefs sync across devices — `hidden_coins`, `fiat`, `theme`. JSON.stringify для объектов. 1024-байт значение, 1024 ключа.
- **DeviceStorage (9.0+)**: локальный кеш до 5 МБ — coin icons base64, последние цены, drafts.
- **SecureStorage (9.0+)**: 10 items, Keychain/Keystore — biometric flag, JWT session, KYC level. `restoreItem(key, cb)` после reinstall.
- **IndexedDB**: TX history cache, price chart данные. Treat как cache, не source of truth.

---

## Web Audio (Apple-Pay success) — iOS quirks

- `AudioContext` стартует в `suspended` на iOS → `resume()` ТОЛЬКО из user-gesture handler
- **ОДИН** AudioContext на сессию, не пересоздавать
- `OscillatorNode + GainNode` envelope (0.1-0.3s) — НЕ `decodeAudioData(mp3)` (-150ms на первом play)
- `statechange` listener → resume на `visibilitychange` (iOS снимает `interrupted` state)
- На WebK/WebA — silent (нет API)
- На iOS 18 есть регрессия: `resume()` возвращает ok, но звука нет. Workaround: пересоздать AudioContext если `state === 'suspended'` после resume.

---

## CSS View Transitions API (мощь)

```js
function switchView(view) {
  if (document.startViewTransition) {
    document.startViewTransition(() => render(view));
  } else {
    render(view);
  }
}
```

```css
::view-transition-old(root) { animation: vt-out 0.18s ease; }
::view-transition-new(root) { animation: vt-in 0.18s ease; }
@keyframes vt-out { to { opacity: 0; transform: translateX(-20px); } }
@keyframes vt-in { from { opacity: 0; transform: translateX(20px); } }
```

Поддержка 2026: iOS Safari 18.1+, Chrome Android 111+, Android WebView 111+. ~85% покрытие.

---

## UI/animation libs (2026 state)

- **GSAP** — **FREE для коммерческого** с апреля 2025 (Webflow acquired). ~50 КБ core. SplitText/MorphSVG/ScrollTrigger всё бесплатно.
- **Motion** (Motion One + Framer Motion merged, 2025) — vanilla path ~3 КБ через WAAPI. Лучше для low-end Android.
- **Anime.js v4** — переписан ESM, tree-shake, spring/bounce physics, TS.
- **Lottie**: для одного checkmark НЕ ставить (30 КБ плеер) → inline SVG.
- **Chart.js → lightweight-charts** (TradingView) для price chart — 45 КБ canvas, идеально для OHLC, smooth on low-end. Chart.js оставить для balance-over-time.
- **QR**: `qr-code-styling` (12.8M weekly) для брендованных, `qrcode-generator` минимал. Для сканирования — `Telegram.WebApp.showScanQrPopup` native.
- **Iconify** — 200k icons on-demand: `<iconify-icon icon="material-symbols:check-circle"></iconify-icon>`.

---

## Performance

- **Service Worker** — НЕ использовать (broken в Telegram iOS WebView, open issue, no ETA)
- LCP < 1.5s — inline critical CSS для header + balance card
- Balance card из server-pushed state (через initData или `<script>window.__INIT__=...</script>`)
- `<link rel="modulepreload" href="/app.js">`
- `<link rel="preload" href="/fonts/Inter-var.woff2" as="font" type="font/woff2" crossorigin>`, `font-display: swap`
- `loading="lazy"` на coin icons ниже фолда
- Avoid `@import` chains в CSS — flatten в один bundle
- SVGO на все SVG; <1 КБ — inline, >1 КБ — `<use href="sprite.svg#id">`

---

## Stars (XTR) payments inside Mini App

**Backend (aiogram)**:
```python
link = await bot.create_invoice_link(
    title="Buy 100 USDT",
    description="P2P order #12345",
    payload="order:12345",        # opaque
    provider_token="",            # ОБЯЗАТЕЛЬНО пусто для Stars
    currency="XTR",
    prices=[LabeledPrice(label="100 USDT", amount=500)],  # 500 Stars
)
```

**Frontend**:
```js
tg.openInvoice(link, status => {
  if (status === 'paid') {
    tg.HapticFeedback.notificationOccurred('success');
    showApplePayCheckmark();
  }
});
```

**Pre-checkout handler** обязан ответить за 10s.
`SuccessfulPayment.telegram_payment_charge_id` → сохранить для refund.
**Refund**: `await bot.refund_star_payment(user_id, charge_id)`.

---

## TOP-15 ошибок которых избегать

1. **Long max-age** для initData на withdraw — должно быть ≤300с + nonce
2. **`openLink` для `tg://` URLs** — закроет app silently. Использовать `openTelegramLink`.
3. **Hash routing fights launch params** — кешировать в sessionStorage ДО роутера
4. **`initDataUnsafe.user` для auth** — НИКОГДА, спуфится в DevTools
5. **`sendData()` для withdraw** — закрывает app + работает только из KeyboardButton
6. **Биометрия без `isVersionAtLeast('7.2')`** — silent no-op, secure withdraw обходится
7. **`prompt()`/`alert()`** — в Electron-wrapped (наш JARVIS!) глотается, iOS фризит. Только `showPopup`.
8. **Resize canvases на каждый viewport_changed** — debounce на `is_state_stable === true`
9. **Log initData в Sentry/Railway logs** — bearer token до expiry. Фильтровать.
10. **Bot token client-side** — никогда
11. **`expand()` + сразу `innerHeight`** — нестабильно. Использовать `viewportStableHeight` или ждать первый stable viewportChanged.
12. **`postMessage('*')` в iframe transport** — указать `https://web.telegram.org` точно
13. **`<audio src="mp3">`** — fetch+decode 300ms delay. Web Audio synth.
14. **View Transitions без fallback** — упадёт на старых клиентах
15. **`try_instant_view: true`** для explorer URLs (Tronscan/Etherscan) — ломает JS-rendered SPA, страница не загрузится

---

## P2P-wallet UX patterns (что Wallet @wallet и @CryptoBot делают)

- НИКОГДА fullscreen — только BottomSheet `expand()`
- Header — цветная "карта" блок с балансом
- Coins — вертикальный список, категории-pills горизонтальные
- MainButton ТОЛЬКО для primary action на screen (Buy/Send/Deposit), НЕ для "Continue" в multi-step (там in-app кнопка)
- `disableVerticalSwipes()` на launch
- `enableClosingConfirmation()` ТОЛЬКО в активном withdraw flow
- Идемпотентность: UUID idempotency key на клиенте → unique-index на backend
- Network picker всегда ЯВНЫЙ (TRC20 vs ERC20 etc.), НИКАКОГО auto-detect — потеря денег
- Pending tx с tronscan ссылкой сразу как только hash есть

---

## Связано

- pride-outkup-service/miniapp/index.html — текущая реализация
- pride-outkup-service/ROADMAP.md — что сделано / что осталось
- workchat-bot/api.py — webhook endpoint для @PrideP2P_bot
