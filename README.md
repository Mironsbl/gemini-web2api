# 🚀 gemini-web2api

<p align="center">
  <img src="logo.png" width="180" alt="gemini-web2api logo">
</p>

<p align="center">
  <a href="README_CN.md">中文文档</a> | 
  <a href="#-interactive-cli">Interactive CLI</a> | 
  <a href="#-web-dashboard">Web Dashboard</a> | 
  <a href="#-features">Features</a>
</p>

Convert Google Gemini's web interface into a premium, OpenAI-compatible API proxy server. Fully equipped with **persistent SQLite caches**, **native macOS cookie decryption**, **multi-user overrides**, **loop evasion mechanisms**, and a **gorgeous interactive CLI and Web Dashboard**.

---

## ⚡ Quick Start & Installation

Install the entire stack (dependencies, launchd daemon, and global command-line shortcut) in **one command**:

```bash
./install.sh
```

This script:
1. Detects macOS environment parameters.
2. Initializes a local Python virtual environment (`venv`).
3. Installs required libraries (`rich`, `prompt_toolkit`, `openai`, `httpx`, `curl_cffi`).
4. Generates a template `config.json` (if missing).
5. Registers and launches the background daemon (`com.miron.gemini-web2api` on port `8081`).
6. Creates a global launcher shortcut: **`gemini-cli`**.

Now run the interactive CLI globally:
```bash
gemini-cli
```

---

## 🖥️ Interactive CLI

Type `gemini-cli` from any folder to enter a state-of-the-art developer terminal interface:

- **Autocompleter**: Real-time autocomplete suggestions for all slash commands and upstream models.
- **Diagnostics Panel**: Connection status, proxy latency, and database record metrics are checked automatically on startup.
- **Dynamic Status Bar**: Displays current active model, conversation thread, YOLO mode status, and connection health.
- **Advanced Slash Commands**:
  - `/help`: Detailed command guide.
  - `/system`: View proxy SQLite records count and tail live daemon logs.
  - `/search <query>`: Directly query the agent's semantic memory database.
  - `/yolo`: Toggle automatic tool execution on the server without confirmations.
  - `/override [cookie|authuser]`: Swap active user cookies or index on the fly.
  - `/history`: Tabulate current conversation statistics.
  - `/export [file.md]`: Save the current log directly to markdown.

---

## 📊 Web Dashboard

Open the local dashboard to monitor the proxy status, logs, and configurations:

```bash
open dashboard.html
```

- **Live Activity Logs**: View requests, timestamps, HTTP status codes, and models.
- **System Diagnostics**: Memory database records count, active threads count, and CPU/host system stats.
- **Model Configs**: Instantly see which models are mapped to the proxy.
- **Credential Overrides**: Test session keys, cookies, and user indexes directly from the browser.

---

## ✨ Features

- **Optional API Keys**: Bearer token authentication configured via `api_keys` in `config.json`.
- **OpenAI Drop-In Compatibility**: Supports `/v1/chat/completions` and `/v1/models`.
- **Advanced Agent Autonomy Tools**: Built-in endpoints to run local sandbox commands, Playwright layout renders, AST code analysis, and system process inspection.
- **Native Cookie Decryption**: Automatically retrieves authenticated session cookies directly from your macOS Google Chrome profile.
- **Multi-User Overrides**: Pass `X-Gemini-Cookie` and `X-Gemini-AuthUser` headers to swap active accounts on demand.
- **Thread ID Context Cache**: Persistent conversation state tracking using `threads.db` for robust multi-turn dialog.
- **Loop Evasion Safeguards**: Instantly detects and intercepts infinite loops when agents repeatedly execute matching tool calls.
- **Adjustable Thinking Depth**: Control reasoning depth by appending `@think=N` (0 to 4) to the model ID.

---

## 🤖 Available Models

| Model | Description | Output Size |
|-------|-------------|-------------|
| `gemini-3.5-flash-thinking` | Deep reasoning mode, longest response | **~20k chars** |
| `gemini-3.5-flash` | Ultra-fast general purpose | ~12k chars |
| `gemini-3.1-pro` | High performance (requires cookie routing) | ~12k chars |
| `gemini-3.5-flash-thinking-lite` | Adaptive thinking depth | ~15k chars |
| `gemini-flash-lite` | Lightweight fast model | ~10k chars |
| `gemini-auto` | Automatic model selection | Varies |

---

## ⚙️ Configuration

A standard `config.json` structure:

```json
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
  "cookie_file": "./cookie.txt",
  "proxy": null,
  "require_command_approval": false,
  "gemini_api_key": null,
  "system_prompt": "autonomous_developer.md",
  "log_requests": true
}
```

---

## 🐳 Docker Deployment

To build and run as a standalone container:

```bash
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json gemini-web2api
```

---

## 🛡️ License

MIT License. Designed for offensive research emulation and anti-cheat telemetry validation.
