/*!
 * PrideSecurity — фронтенд-модуль безопасности P2P (PIN / Biometric / re-auth).
 * Спецификация: docs/p2p/03_INFRA_SECURITY_BOT.md §6.
 *
 * Восстановлен отдельным файлом (а не правкой 583KB index.html), чтобы не зависеть
 * от обрезания больших файлов mount-кэшем (см. ROADMAP).
 *
 * Подключение:  <script src="pride-security.js"></script>  (после telegram-web-app.js)
 *
 * Что делает:
 *   - PIN setup wizard (4 шага), PBKDF2-SHA256 100k + 16b salt, хранение в
 *     Telegram CloudStorage с fallback в localStorage.
 *   - Brute-force lockout: 5 неверных → блок 60с с countdown (sessionStorage).
 *   - Biometric (Telegram.WebApp.BiometricManager) опционально.
 *   - Hot cache: после auth — 60с окно без повторного prompt.
 *   - re-auth перед mutate: перехватывает window.fetch и гейтит защищённые
 *     POST/PUT/PATCH/DELETE на /api/ (skip-list для частых read/typing/read-receipt).
 *
 * ВАЖНО: это UX-слой удобства. Backend (HMAC init_data, idempotency, RBAC, locks)
 * остаётся источником истины и сам отбивает несанкционированные вызовы.
 */
(function () {
  'use strict';
  if (window.PrideSecurity) return; // идемпотентность

  var TG = (window.Telegram && window.Telegram.WebApp) || null;
  var LS = window.localStorage;
  var SS = window.sessionStorage;

  var K_HASH = 'pride_pin_hash';
  var K_SALT = 'pride_pin_salt';
  var K_ATTEMPTS = 'pride_pin_attempts';
  var K_LOCK = 'pride_pin_lock_until';
  var K_BIO = 'pride_pin_biometric';

  var AUTH_TTL_MS = 60 * 1000;   // hot cache 60с
  var LOCK_MS = 60 * 1000;       // lockout 60с
  var MAX_ATTEMPTS = 5;
  var PIN_LEN = 4;

  // ── helpers ──────────────────────────────────────────────────────────
  function haptic(kind) {
    try { TG && TG.HapticFeedback && TG.HapticFeedback.notificationOccurred(kind); } catch (_) {}
  }
  function hapticImpact(style) {
    try { TG && TG.HapticFeedback && TG.HapticFeedback.impactOccurred(style); } catch (_) {}
  }
  function b64(u8) { var s = ''; for (var i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]); return btoa(s); }
  function unb64(s) { return Uint8Array.from(atob(s), function (c) { return c.charCodeAt(0); }); }

  // ── CloudStorage (async) + localStorage fallback ─────────────────────
  function cloudGet(key) {
    return new Promise(function (res) {
      if (TG && TG.CloudStorage && TG.CloudStorage.getItem) {
        try { TG.CloudStorage.getItem(key, function (e, v) { res(e ? null : (v || null)); }); }
        catch (_) { res(null); }
      } else res(null);
    });
  }
  function cloudSet(key, val) {
    return new Promise(function (res) {
      if (TG && TG.CloudStorage && TG.CloudStorage.setItem) {
        try { TG.CloudStorage.setItem(key, val, function () { res(); }); } catch (_) { res(); }
      } else res();
    });
  }
  async function loadCreds() {
    var hash = await cloudGet(K_HASH);
    var salt = await cloudGet(K_SALT);
    if (!hash || !salt) { try { hash = LS.getItem(K_HASH); salt = LS.getItem(K_SALT); } catch (_) {} }
    return (hash && salt) ? { hash: hash, salt: salt } : null;
  }
  async function saveCreds(hash, salt) {
    await cloudSet(K_HASH, hash);
    await cloudSet(K_SALT, salt);
    try { LS.setItem(K_HASH, hash); LS.setItem(K_SALT, salt); } catch (_) {}
  }
  async function clearCreds() {
    try { TG && TG.CloudStorage && TG.CloudStorage.removeItem && TG.CloudStorage.removeItem(K_HASH, function () {}); } catch (_) {}
    try { TG && TG.CloudStorage && TG.CloudStorage.removeItem && TG.CloudStorage.removeItem(K_SALT, function () {}); } catch (_) {}
    try { LS.removeItem(K_HASH); LS.removeItem(K_SALT); LS.removeItem(K_BIO); } catch (_) {}
  }

  // ── PBKDF2 ───────────────────────────────────────────────────────────
  async function derive(pin, saltBytes) {
    var baseKey = await crypto.subtle.importKey(
      'raw', new TextEncoder().encode(pin), { name: 'PBKDF2' }, false, ['deriveBits']
    );
    var bits = await crypto.subtle.deriveBits(
      { name: 'PBKDF2', salt: saltBytes, iterations: 100000, hash: 'SHA-256' }, baseKey, 256
    );
    return b64(new Uint8Array(bits));
  }
  async function hashPin(pin) {
    var salt = crypto.getRandomValues(new Uint8Array(16));
    var hash = await derive(pin, salt);
    return { hash: hash, salt: b64(salt) };
  }
  function eqConst(a, b) {
    if (a.length !== b.length) return false;
    var r = 0;
    for (var i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return r === 0;
  }
  async function verifyPin(pin, saltB64, expectedHash) {
    var h = await derive(pin, unb64(saltB64));
    return eqConst(h, expectedHash);
  }

  // ── lockout ──────────────────────────────────────────────────────────
  function lockRemaining() { var u = +(SS.getItem(K_LOCK) || 0); return Math.max(0, u - Date.now()); }
  function getAttempts() { return +(SS.getItem(K_ATTEMPTS) || 0); }
  function bumpAttempts() {
    var n = getAttempts() + 1;
    if (n >= MAX_ATTEMPTS) { SS.setItem(K_LOCK, String(Date.now() + LOCK_MS)); SS.setItem(K_ATTEMPTS, '0'); }
    else { SS.setItem(K_ATTEMPTS, String(n)); }
    return n;
  }
  function resetAttempts() { SS.setItem(K_ATTEMPTS, '0'); SS.removeItem(K_LOCK); }

  // ── biometric ────────────────────────────────────────────────────────
  function biometricAvailable() {
    var bm = TG && TG.BiometricManager;
    return !!(bm && bm.isInited && bm.isBiometricAvailable);
  }
  function biometricAuth(reason) {
    return new Promise(function (res) {
      var bm = TG && TG.BiometricManager;
      if (biometricAvailable()) {
        try { bm.authenticate({ reason: reason || 'Подтвердите вход' }, function (ok) { res(!!ok); }); }
        catch (_) { res(false); }
      } else res(false);
    });
  }
  function biometricEnabled() { try { return LS.getItem(K_BIO) === '1'; } catch (_) { return false; } }

  // ── модальный UI (PIN keypad) ────────────────────────────────────────
  function injectStyles() {
    if (document.getElementById('pride-sec-styles')) return;
    var st = document.createElement('style');
    st.id = 'pride-sec-styles';
    st.textContent = [
      '.psec-ov{position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;',
      'background:rgba(6,10,20,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);font-family:inherit}',
      '.psec-card{width:min(360px,92vw);padding:28px 22px;border-radius:24px;text-align:center;color:#eaf0ff;',
      'background:linear-gradient(160deg,rgba(40,52,82,.92),rgba(22,30,52,.92));border:1px solid rgba(120,150,220,.25);',
      'box-shadow:0 24px 60px rgba(0,0,0,.5)}',
      '.psec-title{font-size:18px;font-weight:700;margin:4px 0 6px}',
      '.psec-sub{font-size:13px;opacity:.7;margin-bottom:18px;min-height:18px}',
      '.psec-dots{display:flex;gap:14px;justify-content:center;margin-bottom:22px}',
      '.psec-dot{width:14px;height:14px;border-radius:50%;border:2px solid rgba(150,175,235,.5);transition:.15s}',
      '.psec-dot.on{background:#5b8cff;border-color:#5b8cff;box-shadow:0 0 12px rgba(91,140,255,.7)}',
      '.psec-dot.err{background:#ff5b6e;border-color:#ff5b6e;animation:psecShake .35s}',
      '@keyframes psecShake{0%,100%{transform:translateX(0)}25%{transform:translateX(-7px)}75%{transform:translateX(7px)}}',
      '.psec-keys{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}',
      '.psec-key{height:58px;border-radius:16px;font-size:22px;font-weight:600;color:#eaf0ff;cursor:pointer;',
      'background:rgba(120,150,220,.12);border:1px solid rgba(120,150,220,.18);display:flex;align-items:center;justify-content:center;',
      'user-select:none;transition:.1s}',
      '.psec-key:active{transform:scale(.94);background:rgba(120,150,220,.28)}',
      '.psec-key.wide{font-size:14px}',
      '.psec-bio{margin-top:16px;font-size:14px;color:#8fb0ff;cursor:pointer;display:inline-flex;gap:6px;align-items:center}',
      '.psec-cancel{margin-top:14px;font-size:13px;opacity:.6;cursor:pointer}'
    ].join('');
    document.head.appendChild(st);
  }

  function buildModal(opts) {
    injectStyles();
    var ov = document.createElement('div');
    ov.className = 'psec-ov';
    var dots = '';
    for (var i = 0; i < PIN_LEN; i++) dots += '<div class="psec-dot" data-i="' + i + '"></div>';
    var keys = '';
    var layout = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'bio', '0', 'del'];
    layout.forEach(function (k) {
      if (k === 'bio') keys += '<div class="psec-key wide" data-k="bio">' + (opts.allowBio ? '🟦' : '') + '</div>';
      else if (k === 'del') keys += '<div class="psec-key wide" data-k="del">⌫</div>';
      else keys += '<div class="psec-key" data-k="' + k + '">' + k + '</div>';
    });
    ov.innerHTML =
      '<div class="psec-card">' +
        '<div class="psec-title" id="psec-title">' + opts.title + '</div>' +
        '<div class="psec-sub" id="psec-sub">' + (opts.sub || '') + '</div>' +
        '<div class="psec-dots" id="psec-dots">' + dots + '</div>' +
        '<div class="psec-keys">' + keys + '</div>' +
        (opts.allowBio ? '<div class="psec-bio" id="psec-bio">🔓 Войти по биометрии</div>' : '') +
        (opts.cancelable ? '<div class="psec-cancel" id="psec-cancel">Отмена</div>' : '') +
      '</div>';
    document.body.appendChild(ov);
    return ov;
  }

  function setDots(ov, n, err) {
    var ds = ov.querySelectorAll('.psec-dot');
    for (var i = 0; i < ds.length; i++) {
      ds[i].classList.toggle('on', i < n);
      ds[i].classList.toggle('err', !!err);
    }
  }

  // Базовый ввод PIN. resolve(pin:string|null). null = отмена / biometric-success(спец).
  function readPin(opts) {
    return new Promise(function (resolve) {
      var ov = buildModal(opts);
      var buf = '';
      var sub = ov.querySelector('#psec-sub');

      function close(val) { try { ov.remove(); } catch (_) {} resolve(val); }
      function refresh() { setDots(ov, buf.length, false); }

      ov.addEventListener('click', function (ev) {
        var keyEl = ev.target.closest('.psec-key');
        if (keyEl) {
          var k = keyEl.getAttribute('data-k');
          if (k === 'del') { buf = buf.slice(0, -1); refresh(); hapticImpact('light'); return; }
          if (k === 'bio') { if (opts.allowBio && opts.onBio) opts.onBio(close); return; }
          if (/^[0-9]$/.test(k) && buf.length < PIN_LEN) {
            buf += k; refresh(); hapticImpact('light');
            if (buf.length === PIN_LEN) { var done = buf; buf = ''; close(done); }
          }
          return;
        }
        if (ev.target.id === 'psec-bio' && opts.onBio) { opts.onBio(close); return; }
        if (ev.target.id === 'psec-cancel') { close(null); return; }
      });

      opts.__shake = function (msg) {
        setDots(ov, PIN_LEN, true);
        if (sub && msg) sub.textContent = msg;
        haptic('error');
        setTimeout(function () { buf = ''; refresh(); }, 380);
      };
      opts.__setSub = function (msg) { if (sub) sub.textContent = msg; };
      refresh();
    });
  }

  // ── публичный API ────────────────────────────────────────────────────
  var PrideSecurity = {
    _authCache: { until: 0 },

    isEnrolled: function () { return loadCreds().then(function (c) { return !!c; }); },
    biometricAvailable: biometricAvailable,

    // PIN setup wizard (§6.1). resolve(true) при успехе, (false) при отмене.
    setup: function () {
      return new Promise(function (resolve) {
        var first = null;
        function step1() {
          readPin({ title: 'Создайте PIN', sub: 'Придумайте 4-значный код', cancelable: true })
            .then(function (pin) {
              if (pin == null) return resolve(false);
              first = pin; step2();
            });
        }
        function step2() {
          var o = { title: 'Повторите PIN', sub: 'Введите код ещё раз', cancelable: true };
          readPin(o).then(function (pin) {
            if (pin == null) return resolve(false);
            if (pin !== first) { haptic('error'); return step1(); }
            hashPin(first).then(function (hp) {
              saveCreds(hp.hash, hp.salt).then(step3);
            });
          });
        }
        function step3() {
          if (!biometricAvailable()) { resetAttempts(); return resolve(true); }
          var o = { title: 'Биометрия', sub: 'Включить Face ID / Touch ID?', cancelable: true, allowBio: true,
            onBio: function (close) {
              biometricAuth('Включить биометрию').then(function (ok) {
                try { LS.setItem(K_BIO, ok ? '1' : '0'); } catch (_) {}
                close(null); resetAttempts(); resolve(true);
              });
            } };
          // показываем экран с кнопкой биометрии; «Отмена» = пропустить (PIN уже сохранён)
          readPin(o).then(function () { resetAttempts(); resolve(true); });
        }
        step1();
      });
    },

    // Запрос подтверждения (PIN или biometric). resolve(bool). (§6.6)
    prompt: function (reason) {
      return new Promise(function (resolve) {
        loadCreds().then(function (creds) {
          if (!creds) return resolve(false); // не настроен — нечего проверять
          var rem = lockRemaining();
          var allowBio = biometricAvailable() && biometricEnabled();

          var opts = {
            title: '🔒 Подтверждение',
            sub: reason || 'Введите PIN',
            cancelable: true,
            allowBio: allowBio,
            onBio: function (close) {
              biometricAuth(reason).then(function (ok) {
                if (ok) { resetAttempts(); close(null); finish(true); }
                else { haptic('error'); }
              });
            }
          };

          var finished = false;
          function finish(v) { if (!finished) { finished = true; resolve(v); } }

          function startCountdown() {
            var left = Math.ceil(lockRemaining() / 1000);
            opts.__setSub && opts.__setSub('Слишком много попыток. Подождите ' + left + 'с');
            var t = setInterval(function () {
              var l = Math.ceil(lockRemaining() / 1000);
              if (l <= 0) { clearInterval(t); opts.__setSub && opts.__setSub(reason || 'Введите PIN'); }
              else { opts.__setSub && opts.__setSub('Подождите ' + l + 'с'); }
            }, 500);
          }

          // Единственный экран ввода: цифры → verify, биометрия, отмена → finish(false)
          (function single() {
            var ov = buildModal(opts);
            var buf = '';
            var sub = ov.querySelector('#psec-sub');
            function close(val) { try { ov.remove(); } catch (_) {} handle(val); }
            function refresh() { setDots(ov, buf.length, false); }
            opts.__shake = function (msg) { setDots(ov, PIN_LEN, true); if (sub && msg) sub.textContent = msg; haptic('error'); setTimeout(function () { buf = ''; refresh(); }, 380); };
            opts.__setSub = function (msg) { if (sub) sub.textContent = msg; };
            if (lockRemaining() > 0) startCountdown();
            ov.addEventListener('click', function (ev) {
              var keyEl = ev.target.closest('.psec-key');
              if (keyEl) {
                var k = keyEl.getAttribute('data-k');
                if (k === 'del') { buf = buf.slice(0, -1); refresh(); hapticImpact('light'); return; }
                if (k === 'bio') { if (opts.onBio) opts.onBio(close); return; }
                if (/^[0-9]$/.test(k) && buf.length < PIN_LEN && lockRemaining() <= 0) {
                  buf += k; refresh(); hapticImpact('light');
                  if (buf.length === PIN_LEN) {
                    var pin = buf; buf = ''; refresh();
                    verifyPin(pin, creds.salt, creds.hash).then(function (ok) {
                      if (ok) { resetAttempts(); close(null); finish(true); }
                      else {
                        var nn = bumpAttempts();
                        opts.__shake(lockRemaining() > 0 ? 'Заблокировано на 60с' : 'Неверный PIN (' + nn + '/' + MAX_ATTEMPTS + ')');
                        if (lockRemaining() > 0) startCountdown();
                      }
                    });
                  }
                }
                return;
              }
              if (ev.target.id === 'psec-bio' && opts.onBio) { opts.onBio(close); return; }
              if (ev.target.id === 'psec-cancel') { close('cancel'); return; }
            });
            function handle(val) { if (val === 'cancel') finish(false); }
            refresh();
          })();
        });
      });
    },

    // re-auth gate (§6.6). Возвращает Promise<bool>.
    requireAuth: async function (reason) {
      if (Date.now() < (PrideSecurity._authCache.until || 0)) return true;
      var enrolled = await PrideSecurity.isEnrolled();
      if (!enrolled) return true; // PIN опционален: не настроен → не блокируем
      var ok = await PrideSecurity.prompt(reason);
      if (ok) PrideSecurity._authCache = { until: Date.now() + AUTH_TTL_MS };
      return ok;
    },

    // Сменить / снять PIN
    changePin: async function () {
      var ok = await PrideSecurity.prompt('Подтвердите текущий PIN');
      if (!ok) return false;
      return PrideSecurity.setup();
    },
    disable: async function () {
      var ok = await PrideSecurity.prompt('Подтвердите PIN для отключения');
      if (!ok) return false;
      await clearCreds(); resetAttempts();
      PrideSecurity._authCache = { until: 0 };
      return true;
    },
    invalidate: function () { PrideSecurity._authCache = { until: 0 }; }
  };

  window.PrideSecurity = PrideSecurity;
  window.requireAuth = function (reason) { return PrideSecurity.requireAuth(reason); };

  // ── re-auth перед mutate: перехват window.fetch (§6.6 / §6.7) ─────────
  var MUTATING = /^(POST|PUT|PATCH|DELETE)$/i;
  // защищённые (требуют re-auth)
  var PROTECTED = /\/(advertisements|trades|deals|payment[_-]?methods|reviews|disputes|offers)\b/i;
  // skip-list — частые/read-only мутации
  var SKIP = /(typing|delivered|\/read\b|favorites|messages(\?|$)|mark[_-]?read|sync)/i;

  function reasonFor(url) {
    if (/mark[_-]?paid/i.test(url)) return 'Подтверждение оплаты';
    if (/confirm[_-]?payment|release/i.test(url)) return 'Подтверждение получения';
    if (/cancel/i.test(url)) return 'Отмена сделки';
    if (/dispute/i.test(url)) return 'Открытие спора';
    if (/advertisements/i.test(url)) return 'Создание объявления';
    if (/payment[_-]?methods/i.test(url)) return 'Сохранение реквизитов';
    if (/reviews/i.test(url)) return 'Публикация отзыва';
    if (/trades|deals/i.test(url)) return 'Создание сделки';
    return 'Подтвердите операцию';
  }

  var origFetch = window.fetch ? window.fetch.bind(window) : null;
  if (origFetch) {
    window.fetch = async function (input, init) {
      try {
        var url = (typeof input === 'string') ? input : (input && input.url) || '';
        var method = (init && init.method) || (input && input.method) || 'GET';
        var sameOrigin = url.indexOf('/api/') === 0 || url.indexOf(location.origin + '/api/') === 0
                         || (url.indexOf('://') === -1 && url.indexOf('/api/') !== -1);
        if (sameOrigin && MUTATING.test(method) && PROTECTED.test(url) && !SKIP.test(url)) {
          var ok = await PrideSecurity.requireAuth(reasonFor(url));
          if (!ok) {
            var err = new Error('Операция не подтверждена');
            err.__pride_cancelled = true;
            throw err;
          }
        }
      } catch (e) {
        if (e && e.__pride_cancelled) throw e; // наш блок — пробрасываем
        // любая другая ошибка в гейте не должна ронять запрос
      }
      return origFetch(input, init);
    };
  }
})();
