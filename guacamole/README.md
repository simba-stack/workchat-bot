# Guacamole для PRIDE J.A.R.V.I.S.

Inline RDP-окно для кнопки «Зайти на дедик» в Операционной.

## Что это

Готовый Railway-сервис на базе `flcontainers/guacamole` (Apache Guacamole all-in-one)
плюс лёгкий FastAPI-прокси, который создаёт RDP-подключения на лету по REST API.

Когда в дашборде кликнули «Зайти на дедик» — main app дёргает Guacamole-прокси,
тот логинится как admin, создаёт временное RDP-подключение с IP/login/паролем
из crm_passwords, и возвращает iframe-URL. Дашборд встраивает iframe в модалку
и юзер видит дедик прямо в окне Джарвиса.

## Как развернуть на Railway (5 шагов)

### 1. Создать новый сервис в том же Railway проекте

- Railway → Project → **+ New** → **GitHub Repo** → выбери тот же `simba-stack/workchat-bot`
- Settings → **Root Directory** = `guacamole`
- Settings → **Builder** = `DOCKERFILE`, `Dockerfile Path` = `Dockerfile`

### 2. Set Variables

```
GUAC_ADMIN_USER=guacadmin
GUAC_ADMIN_PASS=<СГЕНЕРИРУЙ_ПАРОЛЬ>            # переименуй дефолтный
PRIDE_GUAC_SECRET=<СГЕНЕРИРУЙ_ДЛИННЫЙ_СЕКРЕТ>  # для авторизации между сервисами
PORT=8080                                        # Railway проставит сам, но на всякий случай
```

**Важно:** `GUAC_ADMIN_PASS` ещё надо поменять в самой Guacamole-БД после первого
старта (через UI или DB). Дефолтный `guacadmin/guacadmin` опасен на проде.

После первого деплоя:
1. Зайти на `https://<guac-domain>/guacamole/`
2. Логин `guacadmin/guacadmin`
3. Settings → Users → guacadmin → Change Password → новый пароль из env
4. В Railway env обновить `GUAC_ADMIN_PASS`

### 3. Generate Public Domain

- Settings → Networking → **Generate Domain**
- Скопируй URL, например `pride-guacamole.up.railway.app`

### 4. Set vars в MAIN сервисе (workchat-bot)

В **основном** сервисе (где крутится bot.py) добавь:

```
GUACAMOLE_URL=https://pride-guacamole.up.railway.app
PRIDE_GUAC_SECRET=<тот_же_что_выше>
```

После этого main service перезапустится, и кнопка «Зайти на дедик» начнёт
работать через inline iframe (вместо скачивания .rdp).

### 5. Test

- Открой дашборд → 🛠 Операционная → найди ЛК где привязан дедик
- Нажми «🖥 Зайти на дедик»
- Должна открыться большая модалка с iframe и активным RDP-сеансом

Если что-то не так — Railway → Guacamole service → View Logs.

## Сетевая модель

```
Browser
  │  POST /api/operational/guacamole_session
  ▼
Main service (api.py)
  │  POST <GUACAMOLE_URL>/api/proxy/create_session
  │  Header: X-Pride-Secret: <PRIDE_GUAC_SECRET>
  ▼
Guacamole service (proxy.py)
  │  POST /guacamole/api/tokens  (admin login)
  │  POST /guacamole/api/session/data/<ds>/connections  (создаём connection)
  │  Возвращает: { url: "/guacamole/#/client/<id>?token=..." }
  ▼
Browser встраивает iframe <iframe src="<GUACAMOLE_URL>/guacamole/#/client/...">
  │  WebSocket к /guacamole/websocket-tunnel
  ▼
guacd → RDP к ded_ip:3389 с ded_login/ded_pass
```

## Безопасность

- `PRIDE_GUAC_SECRET` защищает прокси от внешнего доступа (только main app может
  создавать сессии). Меняй раз в квартал.
- `ignore-cert: true` в proxy.py — мы не проверяем сертификат дедика. На проде с
  настоящим Let'sEncrypt можно убрать.
- `enable-drive`, `enable-printing` отключены — нет shared drive/принтеров с
  браузера на дедик (чтобы клиент не утянул что-то через буфер).

## Ресурсы

- ~600-900 MB RAM в стабильном режиме (Tomcat + guacd + nginx + python)
- ~1 GB диск (Postgres внутри образа + логи)
- На Railway Hobby plan ($5/мес за 8GB RAM) уверенно влезает

## Troubleshooting

**Контейнер не стартует** — Tomcat медленно поднимается на холодном старте,
дай 60-90 сек. Если за это время healthcheck не прошёл — увеличь
`healthcheckTimeout` в railway.json.

**`/api/proxy/health` отдаёт 503** — проверь логи: чаще всего `Guacamole login
failed` потому что `GUAC_ADMIN_PASS` в env не совпадает с тем что в Guacamole-БД.
Поменяй пароль в Guacamole UI и обнови env.

**Iframe пустой / не подключается** — RDP-порт 3389 на дедике закрыт со стороны
Railway-cloud, либо ded_ip недоступен. Проверь с любого linux-хоста:
`nc -vz <ded_ip> 3389`.

**`502 guac create_connection failed`** — обычно data_source mismatch. Логи прокси
покажут `data_source=postgresql/mysql/sqlite`. Должен совпадать с тем что в
бандле. Если не работает — попробуй явно задать `GUAC_DATA_SOURCE=postgresql`.
