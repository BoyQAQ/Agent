"""最小 MCP 服务器 —— 演示用，提供两个工具"""
import sys
import json


def handle_request(request):
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "demo-server", "version": "1.0.0"},
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
            {
                "name": "get_time",
                "description": "获取当前服务器时间",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "echo",
                "description": "回显输入文本",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "要回显的文本"}},
                    "required": ["text"],
                },
            },
        ]}}

    if method == "tools/call":
        tool = request["params"]["name"]
        args = request["params"].get("arguments", {})

        if tool == "get_time":
            from datetime import datetime
            result = datetime.now().isoformat()
        elif tool == "echo":
            result = args.get("text", "")
        else:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {tool}"}}

        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": result}],
        }}

    # 通知（无 id），不发响应
    if "id" not in request:
        return None

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            # 通知不回复
            if request.get("method") == "notifications/initialized":
                pass
        except Exception as e:
            err = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32603, "message": str(e)}}
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
