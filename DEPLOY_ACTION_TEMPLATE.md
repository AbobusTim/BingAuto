# GitHub Actions Deploy Template

Заполни в GitHub репозитории: `Settings -> Secrets and variables -> Actions -> New repository secret`

## Обязательные Secrets

- `SERVER_HOST`  
  Значение: `45.32.52.125`

- `SERVER_USER`  
  Значение: `root`

- `SERVER_PORT`  
  Значение: `22`

- `SERVER_SSH_KEY`  
  Вставь **содержимое** приватного ключа `~/.ssh/abober-server-key.pem` (весь текст с `-----BEGIN ...-----` до `-----END ...-----`)

- `SERVER_APP_DIR`  
  Пример: `/opt/bingx-bot`

- `DEPLOY_REPO_URL`  
  Значение: `https://github.com/AbobusTim/BingAuto.git`

- `DEPLOY_BRANCH`  
  Значение: `main`

## Как запускать

1. Открой вкладку `Actions`
2. Выбери workflow `Deploy (Template)`
3. Нажми `Run workflow`

## Важно

В файле workflow сейчас стоит заглушка на рестарт сервиса.  
Отредактируй `.github/workflows/deploy.yml` и замени на свою команду, например:

```bash
systemctl restart bingx-bot.service
```
