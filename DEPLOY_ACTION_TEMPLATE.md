# GitHub Actions Deploy Template

Заполни в GitHub репозитории: `Settings -> Secrets and variables -> Actions -> New repository secret`

## Обязательные Secrets

- `SSH_HOST`  
  Значение: `45.32.52.125`

- `SSH_USER`  
  Значение: `root`

- `SSH_PORT`  
  Значение: `22`

- `SSH_PRIVATE_KEY`  
  Вставь **содержимое** приватного ключа `~/.ssh/abober-server-key.pem` (весь текст с `-----BEGIN ...-----` до `-----END ...-----`)

- `DEPLOY_PATH`  
  Пример: `/opt/bingx-bot`

- `SYSTEMD_SERVICE`  
  Пример: `k-zero` или `bingx-bot`

- `SYSTEMD_USER`  
  Пример: `root`

- `APP_ENV_BASE64`  
  Base64 от содержимого `.env` файла (опционально, если нужно выкатывать env через Actions)

## Как запускать

1. Открой вкладку `Actions`
2. Выбери workflow `Deploy (Template)`
3. Нажми `Run workflow`

## Важно

Workflow уже умеет:
1. Обновить код
2. Переустановить зависимости
3. Распаковать `.env` из `APP_ENV_BASE64` (если заполнен)
4. Перезапустить `systemd` сервис (если заполнен `SYSTEMD_SERVICE`)
