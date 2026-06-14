#!/bin/bash
set -e

# Цвета для вывода в терминал
GREEN='\033[92m'
YELLOW='\033[93m'
RED='\033[91m'
NC='\033[0m'

echo -e "${YELLOW}[*] Начинаем установку gemini-web2api...${NC}"

# 1. Проверка ОС (macOS)
if [ "$(uname)" != "Darwin" ]; then
    echo -e "${RED}[!] Этот скрипт установки предназначен только для macOS.${NC}"
    exit 1
fi

# Получаем абсолютный путь к директории проекта
WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo -e "${GREEN}[*] Директория проекта: $WORKSPACE_DIR${NC}"

# 2. Создание виртуального окружения python
if [ ! -d "$WORKSPACE_DIR/venv" ]; then
    echo -e "${YELLOW}[*] Создание виртуального окружения Python...${NC}"
    python3 -m venv "$WORKSPACE_DIR/venv"
fi

# 3. Установка зависимостей
echo -e "${YELLOW}[*] Установка зависимостей через pip...${NC}"
"$WORKSPACE_DIR/venv/bin/pip" install --upgrade pip
if [ -f "$WORKSPACE_DIR/requirements.txt" ]; then
    "$WORKSPACE_DIR/venv/bin/pip" install -r "$WORKSPACE_DIR/requirements.txt"
fi
"$WORKSPACE_DIR/venv/bin/pip" install prompt-toolkit rich openai httpx curl_cffi

# 4. Проверка и создание config.json
if [ ! -f "$WORKSPACE_DIR/config.json" ]; then
    echo -e "${YELLOW}[*] Создание дефолтного config.json...${NC}"
    cat << EOF > "$WORKSPACE_DIR/config.json"
{
  "port": 8081,
  "host": "0.0.0.0",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
  "auth_user": 1,
  "xsrf_token": null,
  "default_model": "gemini-3.5-flash-thinking",
  "api_keys": ["sk-test-key"],
  "cookie_file": "$WORKSPACE_DIR/cookie.txt",
  "proxy": null,
  "require_command_approval": false,
  "gemini_api_key": null,
  "system_prompt": "autonomous_developer.md",
  "log_requests": true
}
EOF
fi

# 5. Создание com.miron.gemini-web2api.plist для launchd
echo -e "${YELLOW}[*] Настройка демона launchd...${NC}"
mkdir -p ~/Library/LaunchAgents

cat << EOF > ~/Library/LaunchAgents/com.miron.gemini-web2api.plist
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.miron.gemini-web2api</string>
    <key>ProgramArguments</key>
    <array>
        <string>$WORKSPACE_DIR/venv/bin/python</string>
        <string>$WORKSPACE_DIR/gemini_web2api.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>$WORKSPACE_DIR</string>
    <key>StandardOutPath</key>
    <string>$WORKSPACE_DIR/gemini_web2api.log</string>
    <key>StandardErrorPath</key>
    <string>$WORKSPACE_DIR/gemini_web2api_err.log</string>
</dict>
</plist>

# Перезагружаем службу в launchd
launchctl unload ~/Library/LaunchAgents/com.miron.gemini-web2api.plist >/dev/null 2>&1 || true
launchctl load ~/Library/LaunchAgents/com.miron.gemini-web2api.plist >/dev/null 2>&1 || true
launchctl start com.miron.gemini-web2api >/dev/null 2>&1 || true

# 6. Создание исполняемого лаунчера gemini-cli в ~/.local/bin
echo -e "${YELLOW}[*] Создание исполняемого файла в ~/.local/bin/gemini-cli...${NC}"
mkdir -p ~/.local/bin

cat << EOF > ~/.local/bin/gemini-cli
#!/bin/bash

PORT=8081
if [ -f "$WORKSPACE_DIR/config.json" ]; then
    PORT_VAL=\$(grep -o '"port": *[0-9]*' "$WORKSPACE_DIR/config.json" | grep -o '[0-9]*')
    if [ ! -z "\$PORT_VAL" ]; then
        PORT=\$PORT_VAL
    fi
fi

# Проверка, запущен ли прокси-сервер
if ! nc -z localhost \$PORT >/dev/null 2>&1; then
    echo -e "\033[93m[*] Прокси-сервер на порту \$PORT не запущен. Запускаем службу...\033[0m"
    launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.miron.gemini-web2api.plist >/dev/null 2>&1
    launchctl start com.miron.gemini-web2api >/dev/null 2>&1
    
    for i in {1..10}; do
        if nc -z localhost \$PORT >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
    
    if ! nc -z localhost \$PORT >/dev/null 2>&1; then
        echo -e "\033[91m[!] Не удалось запустить фоновую службу прокси. CLI запускается в offline-режиме.\033[0m"
    else
        echo -e "\033[92m[*] Фоновая служба прокси успешно запущена.\033[0m"
    fi
fi

# Запуск CLI клиента
exec "$WORKSPACE_DIR/venv/bin/python" "$WORKSPACE_DIR/gemini_agent_cli.py" "\$@"
EOF

chmod +x ~/.local/bin/gemini-cli

# Создание исполняемого лаунчера gemini-official для запуска оригинального CLI
echo -e "${YELLOW}[*] Создание исполняемого файла в ~/.local/bin/gemini-official...${NC}"
cat << EOF > ~/.local/bin/gemini-official
#!/bin/bash
export GOOGLE_GEMINI_BASE_URL="http://localhost:8081"
export GEMINI_API_KEY="sk-test-key"
export NO_PROXY="localhost,127.0.0.1,\$NO_PROXY"
export no_proxy="localhost,127.0.0.1,\$no_proxy"
exec npx -y @google/gemini-cli "\$@"
EOF
chmod +x ~/.local/bin/gemini-official

# 7. Проверка PATH
echo -e "${GREEN}[*] Установка завершена успешно!${NC}"
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo -e "${YELLOW}[!] ВНИМАНИЕ: ~/.local/bin отсутствует в вашей переменной PATH.${NC}"
    echo -e "Добавьте следующую строку в ваш ~/.zshrc (или ~/.bash_profile):"
    echo -e "  ${GREEN}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}"
else
    echo -e "Интерактивный CLI:   ${GREEN}gemini-cli${NC}"
    echo -e "Официальный CLI:     ${GREEN}gemini-official${NC}"
fi
