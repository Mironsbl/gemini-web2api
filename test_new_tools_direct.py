import os
import sys
import importlib.util

# Load gemini_web2api.py root script dynamically to avoid folder name collision
script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_web2api.py")
spec = importlib.util.spec_from_file_location("gemini_web2api_root", script_path)
gemini_web2api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gemini_web2api)

run_local_tool = gemini_web2api.run_local_tool
GLOBAL_MEMORY = gemini_web2api.GLOBAL_MEMORY

def test_tool(name, args):
    print(f"\n=== Testing Tool: {name} ===")
    print(f"Args: {args}")
    try:
        res = run_local_tool(name, args)
        print(f"Result Status: {res.get('status')}")
        if 'error' in res or res.get('status') == 'error':
            print(f"Error: {res}")
        else:
            # Print truncated results to keep output clean
            for k, v in res.items():
                val_str = str(v)
                if len(val_str) > 200:
                    val_str = val_str[:200] + "..."
                print(f"  {k}: {val_str}")
    except Exception as e:
        print(f"Exception raised: {e}")

if __name__ == "__main__":
    # 1. Test execute_sandboxed_command
    test_tool("execute_sandboxed_command", {"command": "echo 'Hello from Sandbox!'"})

    # 2. Test web_inspect_screenshot
    test_tool("web_inspect_screenshot", {"url": "http://localhost:8081/"})

    # 3. Test analyze_code_symbols
    test_tool("analyze_code_symbols", {"filepath": "./test_new_tools_direct.py"})

    # 4. Test semantic_memory_store
    test_tool("semantic_memory_store", {"content": "The secret key is ANTIGRAVITY_PERFECT_AGENT", "tags": "secrets key"})

    # 5. Test semantic_memory_search
    test_tool("semantic_memory_search", {"query": "ANTIGRAVITY_PERFECT_AGENT"})

    # 6. Test run_self_debug_loop
    test_tool("run_self_debug_loop", {"test_command": "echo 'Tests passing!'"})
    test_tool("run_self_debug_loop", {"test_command": "false"}) # Should return failed status

    # 7. Test inspect_system_process
    test_tool("inspect_system_process", {"filter_name": "python"})
