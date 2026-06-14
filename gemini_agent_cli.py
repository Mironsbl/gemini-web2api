#!/usr/bin/env python3
import os
import sys
import json
import argparse
import time
import uuid
import sqlite3
import shutil
from openai import OpenAI
import httpx

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.markdown import Markdown
    from rich.syntax import Syntax
    from rich.text import Text
    from rich.live import Live
    from rich.align import Align
    from rich.columns import Columns
    from rich.box import ROUNDED, DOUBLE
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style
    from prompt_toolkit.formatted_text import HTML
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

# Fallback color codes if Rich is missing
class Color:
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    BOLD = '\033[1m'
    END = '\033[0m'

BANNER = """
   ██████  ███████ ███    ███ ██ ███    ██ ██   █████  ██████  ██ 
  ██       ██      ████  ████ ██ ████   ██ ██  ██   ██ ██   ██ ██ 
  ██   ███ █████   ██ ████ ██ ██ ██ ██  ██ ██  ███████ ██████  ██ 
  ██    ██ ██      ██  ██  ██ ██ ██  ██ ██ ██  ██   ██ ██      ██ 
   ██████  ███████ ██      ██ ██ ██   ████ ██  ██   ██ ██      ██ 
                                                                  
      WEB2API PROXY INTERACTIVE AGENT CLIENT — OFFSEC EDITION
"""

# Custom autocomplete for slash commands and sub-arguments
class GeminiCLICompleter(Completer):
    def __init__(self, models_list=None):
        self.models_list = models_list or [
            "gemini-3.5-flash-thinking",
            "gemini-3.5-flash",
            "gemini-3.1-pro",
            "gemini-auto",
            "gemini-3.5-flash-thinking-lite",
            "gemini-flash-lite"
        ]
        self.commands = {
            "/help": "Show command guide and active configurations",
            "/clear": "Clear current chat memory and start new thread ID",
            "/model": "Switch model or list models (e.g. /model gemini-3.5-flash)",
            "/yolo": "Toggle automatic tool execution on the server (YOLO mode)",
            "/override": "Override request headers (e.g. /override cookie <val>)",
            "/system": "Display proxy logs, memory database stats, and thread cache info",
            "/search": "Search in the local semantic memory SQLite database",
            "/history": "View current interactive conversation log and token metrics",
            "/export": "Export current chat history to markdown file",
            "/exit": "Safely exit interactive chat session"
        }
        self.override_sub = ["cookie", "authuser", "reset"]

    def get_completions(self, document, complete_event):
        text = document.text
        words = text.split()
        if not words:
            # List all commands
            for cmd, desc in self.commands.items():
                yield Completion(cmd, start_position=0, display_meta=desc)
            return

        first_word = words[0].lower()

        # If user is typing the command name
        if len(words) == 1 and text.startswith("/"):
            for cmd, desc in self.commands.items():
                if cmd.startswith(first_word):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)
            return

        # Sub-arguments autocomplete
        if first_word == "/model" and len(words) >= 2:
            typed_model = words[1]
            start_pos = -len(typed_model) if len(words) == 2 else 0
            for model in self.models_list:
                if model.lower().startswith(typed_model.lower()):
                    yield Completion(model, start_position=start_pos)
            return

        if first_word == "/override" and len(words) >= 2:
            typed_sub = words[1]
            if len(words) == 2:
                for sub in self.override_sub:
                    if sub.startswith(typed_sub.lower()):
                        yield Completion(sub, start_position=-len(typed_sub))
            return


def count_memory_db_records(db_path="memory.db"):
    if not os.path.exists(db_path):
        return 0
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM memory")
            return cursor.fetchone()[0]
    except Exception:
        return 0

def count_threads_db_records(db_path="threads.db"):
    if not os.path.exists(db_path):
        return 0
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM threads")
            return cursor.fetchone()[0]
    except Exception:
        return 0

def query_semantic_memories(db_path="memory.db", query="", limit=10):
    if not os.path.exists(db_path):
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, content, tags, timestamp FROM memory")
            rows = cursor.fetchall()
        
        if not query:
            return rows[-limit:]
            
        q_words = set(query.lower().split())
        results = []
        for rid, content, tags, ts in rows:
            c_words = content.lower().split()
            overlap = len(q_words.intersection(c_words))
            if overlap > 0:
                results.append((rid, content, tags, ts, overlap))
        results.sort(key=lambda x: x[4], reverse=True)
        return [r[:4] for r in results[:limit]]
    except Exception as e:
        return []

def main():
    parser = argparse.ArgumentParser(description="Gemini Proxy Interactive CLI Client")
    parser.add_argument("--model", "-m", type=str, default=None, help="Force override default model")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming response")
    parser.add_argument("--yolo", action="store_true", help="Start session in YOLO mode")
    args = parser.parse_args()

    # Create console
    console = Console() if HAS_RICH else None

    # Load configuration
    config_path = "./config.json"
    api_key = "sk-test-key"
    base_url = "http://localhost:8081/v1"
    default_model = "gemini-3.5-flash-thinking"
    
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
                if cfg.get("api_keys"):
                    api_key = cfg["api_keys"][0]
                port = cfg.get("port", 8081)
                host = cfg.get("host", "localhost")
                if host == "0.0.0.0":
                    host = "localhost"
                base_url = f"http://{host}:{port}/v1"
                if cfg.get("default_model"):
                    default_model = cfg["default_model"]
        except Exception:
            pass

    selected_model = args.model or default_model
    stream_mode = not args.no_stream
    yolo_mode = args.yolo

    # Custom override variables
    cookie_override = None
    auth_user_override = None
    thread_id = f"cli-thread-{uuid.uuid4().hex[:10]}"

    # Fetch model list from server to verify health
    models_list = []
    ping_status = "OFFLINE"
    latency = 0.0

    try:
        t0 = time.time()
        resp = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0
        )
        latency = (time.time() - t0) * 1000.0
        if resp.status_code == 200:
            ping_status = "ONLINE"
            data = resp.json()
            models_list = [m["id"] for m in data.get("data", [])]
    except Exception:
        pass

    if HAS_RICH:
        # Display Banner
        console.print(Panel(Align.center(Text(BANNER, style="bold cyan")), border_style="cyan", box=DOUBLE))
        
        # Display connection status
        status_table = Table(title="Connection Diagnostics", show_header=True, header_style="bold magenta", box=ROUNDED)
        status_table.add_column("Parameter", style="cyan")
        status_table.add_column("Value", style="green")
        
        status_table.add_row("Proxy API Base", base_url)
        status_table.add_row("Connection Status", f"[bold green]ONLINE[/bold green]" if ping_status == "ONLINE" else "[bold red]OFFLINE (check local daemon)[/bold red]")
        status_table.add_row("Response Latency", f"{latency:.1f} ms" if ping_status == "ONLINE" else "N/A")
        status_table.add_row("Active Model", selected_model)
        status_table.add_row("Streaming Mode", "Enabled" if stream_mode else "Disabled")
        status_table.add_row("YOLO Mode", "[bold yellow]ENABLED (bypass tool confirmations)[/bold yellow]" if yolo_mode else "DISABLED (server prompts user)")
        status_table.add_row("Active Thread ID", thread_id)
        
        console.print(status_table)
        console.print("\n[bold yellow]Type [bold red]/help[/bold red] to view guide. Finish line with [bold cyan]\\ [/bold cyan] for multiline prompts.[/bold yellow]\n")
    else:
        print(f"{Color.CYAN}{BANNER}{Color.END}")
        print(f"Proxy API Base: {base_url}")
        print(f"Connection Status: {ping_status}")
        print(f"Active Model: {selected_model}")
        print(f"Active Thread: {thread_id}")
        print("Type /help to view command options.\n")

    client = OpenAI(base_url=base_url, api_key=api_key)

    # Initialize prompt_toolkit session
    completer = GeminiCLICompleter(models_list)
    session = None
    if HAS_PROMPT_TOOLKIT:
        # Style prompt_toolkit UI components
        pt_style = Style.from_dict({
            'prompt': 'ansiwhite bold',
            'model': 'ansicyan bold',
            'arrow': 'ansiyellow bold',
            'bottom-toolbar': 'bg:#1e1e1e #888888',
            'bottom-toolbar.model': 'ansicyan bold',
            'bottom-toolbar.yolo': 'ansiyellow bold',
            'bottom-toolbar.status': 'ansigreen bold' if ping_status == "ONLINE" else 'ansired bold',
        })
        history_file = os.path.expanduser("~/.gemini_web2api_history")
        session = PromptSession(
            history=FileHistory(history_file),
            completer=completer,
            style=pt_style
        )

    # In-memory session message log
    messages = [
        {
            "role": "system",
            "content": (
                "You are an active developer assistant. You have access to local file system "
                "and shell execution tools. Do NOT instruct the user to run commands; instead, "
                "execute the appropriate tool call immediately."
            )
        }
    ]

    while True:
        # Build status bar content dynamically
        toolbar_text = ""
        if HAS_PROMPT_TOOLKIT:
            yolo_status = "YOLO:ON" if yolo_mode else "YOLO:OFF"
            auth_str = f" | User: {auth_user_override}" if auth_user_override is not None else ""
            cookie_str = " | Cookie: OVERRIDDEN" if cookie_override else ""
            toolbar_text = HTML(
                f" <model>{selected_model}</model> | Thread: {thread_id[:16]}... | "
                f"<yolo>{yolo_status}</yolo>{auth_str}{cookie_str} | Status: <status>{ping_status}</status>"
            )

        # Prompt input collection
        try:
            prompt_input_accumulator = []
            while True:
                if HAS_PROMPT_TOOLKIT:
                    prompt_label = [
                        ('class:model', f"{selected_model}"),
                        ('class:prompt', " @ "),
                        ('class:arrow', "➔ "),
                    ]
                    line = session.prompt(
                        prompt_label,
                        bottom_toolbar=toolbar_text
                    ).strip()
                else:
                    line = input(f"{Color.GREEN}{selected_model}{Color.END} ➔ ").strip()

                if not line:
                    break

                if line.endswith("\\"):
                    # Multiline accumulator
                    prompt_input_accumulator.append(line[:-1].strip())
                    continue
                else:
                    prompt_input_accumulator.append(line)
                    break

            user_input = "\n".join(prompt_input_accumulator).strip()
        except (KeyboardInterrupt, EOFError):
            if HAS_RICH:
                console.print("[bold red]\n[*] Exiting. Session saved.[/bold red]")
            else:
                print("\n[*] Exiting.")
            break

        if not user_input:
            continue

        # Command parser
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd == "/exit" or cmd == "/quit":
                break

            elif cmd == "/clear":
                messages = [messages[0]]
                thread_id = f"cli-thread-{uuid.uuid4().hex[:10]}"
                if HAS_RICH:
                    console.print(Panel("[bold green][*] Conversation memory and thread context reset.[/bold green]", border_style="green"))
                else:
                    print("[*] Conversation memory and thread context reset.")
                continue

            elif cmd == "/help":
                if HAS_RICH:
                    help_table = Table(title="Slash Commands & Configurations", show_header=True, header_style="bold magenta", box=ROUNDED)
                    help_table.add_column("Command", style="cyan")
                    help_table.add_column("Description", style="white")
                    for k, v in completer.commands.items():
                        help_table.add_row(k, v)
                    console.print(help_table)

                    # Print active values
                    active_table = Table(title="Active Session Variables", show_header=True, header_style="bold yellow", box=ROUNDED)
                    active_table.add_column("Property", style="cyan")
                    active_table.add_column("Value", style="green")
                    active_table.add_row("Base URL", base_url)
                    active_table.add_row("Active Model", selected_model)
                    active_table.add_row("Active Thread ID", thread_id)
                    active_table.add_row("Streaming Mode", str(stream_mode))
                    active_table.add_row("YOLO Mode", str(yolo_mode))
                    active_table.add_row("Cookie Override", "Active" if cookie_override else "None")
                    active_table.add_row("AuthUser Override", str(auth_user_override) if auth_user_override is not None else "None")
                    console.print(active_table)
                else:
                    for k, v in completer.commands.items():
                        print(f"  {k} - {v}")
                continue

            elif cmd == "/model":
                if len(parts) > 1:
                    new_model = parts[1].strip()
                    selected_model = new_model
                    if HAS_RICH:
                        console.print(f"[bold green][*] Active model switched to: {selected_model}[/bold green]")
                    else:
                        print(f"[*] Active model switched to: {selected_model}")
                else:
                    # Fetch models and list
                    if models_list:
                        if HAS_RICH:
                            model_table = Table(title="Available Upstream Models", show_header=True, header_style="bold cyan", box=ROUNDED)
                            model_table.add_column("Model ID", style="green")
                            for m in models_list:
                                model_table.add_row(m)
                            console.print(model_table)
                        else:
                            print("Available models:")
                            for m in models_list:
                                print(f"  - {m}")
                    else:
                        print("[!] No models fetched or server offline.")
                continue

            elif cmd == "/yolo":
                yolo_mode = not yolo_mode
                if HAS_RICH:
                    color = "green" if yolo_mode else "yellow"
                    status = "ENABLED (bypassing confirmations)" if yolo_mode else "DISABLED (server prompts user)"
                    console.print(f"[bold {color}][*] YOLO Mode is now {status}.[/bold {color}]")
                else:
                    print(f"[*] YOLO Mode is now {'ENABLED' if yolo_mode else 'DISABLED'}")
                continue

            elif cmd == "/override":
                if len(parts) >= 2:
                    sub = parts[1].lower()
                    if sub == "cookie":
                        if len(parts) > 2:
                            cookie_override = parts[2].strip()
                            if HAS_RICH:
                                console.print("[bold green][*] Custom Cookie Override registered.[/bold green]")
                            else:
                                print("[*] Custom Cookie Override registered.")
                        else:
                            console.print("[bold red][!] Provide cookie string after 'cookie'[/bold red]")
                    elif sub == "authuser":
                        if len(parts) > 2:
                            try:
                                auth_user_override = int(parts[2].strip())
                                if HAS_RICH:
                                    console.print(f"[bold green][*] AuthUser Override set to: {auth_user_override}[/bold green]")
                                else:
                                    print(f"[*] AuthUser Override set to: {auth_user_override}")
                            except ValueError:
                                console.print("[bold red][!] AuthUser must be an integer.[/bold red]")
                        else:
                            console.print("[bold red][!] Provide authuser index after 'authuser'[/bold red]")
                    elif sub == "reset":
                        cookie_override = None
                        auth_user_override = None
                        if HAS_RICH:
                            console.print("[bold green][*] Overrides cleared.[/bold green]")
                        else:
                            print("[*] Overrides cleared.")
                    else:
                        console.print("[bold red][!] Options: cookie, authuser, reset[/bold red]")
                else:
                    console.print("[bold red][!] Usage: /override cookie <val> | authuser <val> | reset[/bold red]")
                continue

            elif cmd == "/system":
                # Check DB stats
                mem_count = count_memory_db_records("memory.db")
                thread_count = count_threads_db_records("threads.db")
                
                # Fetch recent log entries if possible
                log_lines = []
                if os.path.exists("gemini_web2api.log"):
                    try:
                        with open("gemini_web2api.log", "r") as f:
                            log_lines = f.readlines()[-8:]
                    except Exception:
                        pass

                if HAS_RICH:
                    sys_table = Table(title="System & Database Info", show_header=True, header_style="bold magenta", box=ROUNDED)
                    sys_table.add_column("Parameter", style="cyan")
                    sys_table.add_column("Value", style="green")
                    sys_table.add_row("SQLite Memory Records (memory.db)", f"{mem_count} entries")
                    sys_table.add_row("SQLite Active Threads (threads.db)", f"{thread_count} threads")
                    sys_table.add_row("Terminal dimensions", f"{shutil.get_terminal_size()}")
                    sys_table.add_row("Proxy status", f"[bold green]ONLINE[/bold green]" if ping_status == "ONLINE" else "[bold red]OFFLINE[/bold red]")
                    console.print(sys_table)

                    if log_lines:
                        log_panel = Panel(
                            "".join(log_lines),
                            title="Recent Proxy Log Entries",
                            border_style="yellow",
                            box=ROUNDED
                        )
                        console.print(log_panel)
                else:
                    print(f"SQLite Memory Cache: {mem_count} records")
                    print(f"SQLite Thread Cache: {thread_count} threads")
                continue

            elif cmd == "/search":
                if len(parts) > 1:
                    query = parts[1].strip()
                    results = query_semantic_memories("memory.db", query=query, limit=10)
                    if results:
                        if HAS_RICH:
                            res_table = Table(title=f"Semantic Memory Hits for: '{query}'", show_header=True, header_style="bold magenta", box=ROUNDED)
                            res_table.add_column("ID", style="cyan")
                            res_table.add_column("Content", style="white")
                            res_table.add_column("Tags", style="yellow")
                            res_table.add_column("Timestamp", style="green")
                            for rid, content, tags, ts in results:
                                res_table.add_row(str(rid), content[:150] + ("..." if len(content) > 150 else ""), tags or "", ts)
                            console.print(res_table)
                        else:
                            for rid, content, tags, ts in results:
                                print(f"[{rid}] {ts} - {tags}: {content[:100]}")
                    else:
                        console.print("[yellow][!] No memory search matches found.[/yellow]")
                else:
                    console.print("[bold red][!] Provide search term. Usage: /search <term>[/bold red]")
                continue

            elif cmd == "/history":
                if HAS_RICH:
                    hist_table = Table(title="Interactive Conversation Logs", show_header=True, header_style="bold cyan", box=ROUNDED)
                    hist_table.add_column("Role", style="magenta")
                    hist_table.add_column("Content Summary", style="white")
                    hist_table.add_column("Characters", style="green")
                    for idx, msg in enumerate(messages[1:]): # skip system prompt
                        r = msg["role"]
                        cont = msg.get("content") or ""
                        summary = cont[:100] + "..." if len(cont) > 100 else cont
                        # Format tools/call content nicely if present
                        if msg.get("tool_calls"):
                            summary = f"[Tool Calls: {len(msg['tool_calls'])} calls]"
                        elif r == "tool":
                            summary = f"[Tool Return: {msg.get('name')}]"
                        
                        hist_table.add_row(r.capitalize(), summary, str(len(cont)))
                    console.print(hist_table)
                else:
                    for msg in messages[1:]:
                        print(f"{msg['role']}: {msg.get('content')[:100]}...")
                continue

            elif cmd == "/export":
                if len(parts) > 1:
                    filepath = parts[1].strip()
                else:
                    filepath = f"chat_export_{int(time.time())}.md"

                try:
                    with open(filepath, "w") as f:
                        f.write(f"# Chat Session Export\n")
                        f.write(f"- **Model**: {selected_model}\n")
                        f.write(f"- **Thread ID**: {thread_id}\n")
                        f.write(f"- **Timestamp**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                        f.write("---\n\n")
                        for msg in messages[1:]:
                            role = msg["role"].capitalize()
                            content = msg.get("content")
                            if content:
                                f.write(f"### {role}\n{content}\n\n")
                            if msg.get("tool_calls"):
                                f.write(f"### {role} (Tool Calls)\n```json\n{json.dumps(msg['tool_calls'], indent=2)}\n```\n\n")
                    if HAS_RICH:
                        console.print(f"[bold green][*] Successfully exported conversation to: {filepath}[/bold green]")
                    else:
                        print(f"[*] Successfully exported conversation to: {filepath}")
                except Exception as e:
                    console.print(f"[bold red][!] Failed to export: {e}[/bold red]")
                continue

            else:
                if HAS_RICH:
                    console.print(f"[bold red][!] Unknown slash command: {cmd}. Type /help for assistance.[/bold red]")
                else:
                    print(f"[!] Unknown slash command: {cmd}")
                continue

        # Add user turn to conversation history
        messages.append({"role": "user", "content": user_input})

        # Set up custom headers
        headers = {}
        if yolo_mode:
            headers["X-Yolo"] = "true"
        if thread_id:
            headers["X-Thread-ID"] = thread_id
        if cookie_override:
            headers["X-Gemini-Cookie"] = cookie_override
        if auth_user_override is not None:
            headers["X-Gemini-AuthUser"] = str(auth_user_override)

        # Call the OpenAI endpoint with streaming display
        if HAS_RICH:
            console.print()
            try:
                if stream_mode:
                    response = client.chat.completions.create(
                        model=selected_model,
                        messages=messages,
                        stream=True,
                        extra_headers=headers
                    )
                    full_response = ""
                    # We render live Markdown streaming
                    with Live(Text(""), console=console, auto_refresh=True, refresh_per_second=12) as live:
                        for chunk in response:
                            content = chunk.choices[0].delta.content
                            if content:
                                full_response += content
                                # Keep Markdown parsed live
                                live.update(Panel(Markdown(full_response), title=f"[bold green]{selected_model}[/bold green]", border_style="green", box=ROUNDED))
                    
                    messages.append({"role": "assistant", "content": full_response})
                else:
                    # Sync request with spinner
                    with console.status(f"[cyan]Waiting for {selected_model} response...[/cyan]"):
                        resp = client.chat.completions.create(
                            model=selected_model,
                            messages=messages,
                            stream=False,
                            extra_headers=headers
                        )
                    ans = resp.choices[0].message.content or ""
                    # Panel display
                    console.print(Panel(Markdown(ans), title=f"[bold green]{selected_model}[/bold green]", border_style="green", box=ROUNDED))
                    messages.append({"role": "assistant", "content": ans})
                console.print()
                ping_status = "ONLINE" # reset offline marker if call succeeds
            except Exception as e:
                console.print(Panel(f"[bold red][!] Connection or Upstream Error:[/bold red]\n{e}\n\n[yellow]Ensure the proxy server is running: 'com.miron.gemini-web2api'[/yellow]", title="API Error", border_style="red", box=ROUNDED))
                ping_status = "OFFLINE"
        else:
            print(f"\n{Color.CYAN}{selected_model} ➔ {Color.END}", end="", flush=True)
            try:
                if stream_mode:
                    response = client.chat.completions.create(
                        model=selected_model,
                        messages=messages,
                        stream=True,
                        extra_headers=headers
                    )
                    full_response = ""
                    for chunk in response:
                        content = chunk.choices[0].delta.content
                        if content:
                            print(content, end="", flush=True)
                            full_response += content
                    print()
                    messages.append({"role": "assistant", "content": full_response})
                else:
                    resp = client.chat.completions.create(
                        model=selected_model,
                        messages=messages,
                        stream=False,
                        extra_headers=headers
                    )
                    ans = resp.choices[0].message.content or ""
                    print(ans)
                    messages.append({"role": "assistant", "content": ans})
                print()
                ping_status = "ONLINE"
            except Exception as e:
                print(f"\n{Color.RED}[!] Error: {e}{Color.END}\n")
                ping_status = "OFFLINE"

if __name__ == "__main__":
    main()
