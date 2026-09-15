"""1C MCP fleet monitor: Docker state + MCP liveness + index progress. Single file."""
import json
import re
import time
from datetime import datetime, timezone

import docker
import requests
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# Stable fleet. `container=None` means compose-managed pair shown separately.
SERVERS = [
    {"key": "help", "name": "HelpSearch", "container": "1c_help_mcp",
     "port": 8003, "desc": "Справка платформы 1С", "tools": ["docsearch", "docinfo", "formatspec", "standards"]},
    {"key": "graph", "name": "GraphMetadata", "container": "1c_graph_metadata",
     "port": 8006, "desc": "Граф связей (Neo4j)", "tools": ["list_graph_projects", "resolve_effective_entity"]},
    {"key": "codemeta", "name": "CodeMetadata", "container": "1c_code_metadata_mcp",
     "port": 8000, "desc": "Метаданные + BSL-код (28 инструментов)", "tools": ["metadatasearch", "codesearch", "stats"]},
    {"key": "ssl", "name": "SSLSearch", "container": "1c_ssl_mcp",
     "port": 8008, "desc": "Поиск по БСП", "tools": ["ssl_search"]},
    {"key": "templates", "name": "TemplatesSearch", "container": "1c_templates_mcp",
     "port": 8004, "desc": "Шаблоны + память проекта", "tools": ["templatesearch", "remember", "recall"]},
    {"key": "syntax", "name": "SyntaxCheck", "container": "1c_syntaxcheck_mcp",
     "port": 8002, "desc": "Проверка синтаксиса BSL", "tools": ["syntaxcheck"]},
    {"key": "checker", "name": "1CCodeChecker", "container": "1c_code_checker",
     "port": 8007, "desc": "Проверка через 1С:Напарник (нужен токен)", "tools": []},
    {"key": "neo4j", "name": "Neo4j (для Graph)", "container": "neo4j",
     "port": 7474, "desc": "Графовая БД", "tools": [], "no_mcp": True},
]

PROGRESS_RE = re.compile(
    r"([\w\s]{3,40}?progress)\s*:\s*(\d+)\s*/\s*(\d+)[^|\n]*?(?:\|\s*([^\n|]*?))?\s*(?:\|\s*ETA\s*([^\n|]+))?",
    re.IGNORECASE)
ETA_RE = re.compile(r"ETA\s*([^\s|]+)", re.IGNORECASE)


def dclient():
    return docker.from_env()


def container_info(name):
    """State of one container; never raises."""
    try:
        c = dclient().containers.get(name)
    except Exception as e:
        return {"exists": False, "status": "not found", "detail": str(e)[:120]}
    try:
        c.reload()
        st = c.attrs.get("State", {})
        # Health имеет смысл только у запущенного контейнера; у остановленного
        # Docker показывает протухший статус последней проверки — скрываем его.
        health = (st.get("Health") or {}).get("Status") if st.get("Status") == "running" else None
        started = st.get("StartedAt", "")
        uptime = ""
        if st.get("Status") == "running" and started:
            try:
                dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                secs = int((datetime.now(timezone.utc) - dt).total_seconds())
                uptime = f"{secs // 3600}ч {(secs % 3600) // 60}м"
            except Exception:
                pass
        return {"exists": True, "status": st.get("Status", "?"),
                "health": health, "restarts": st.get("RestartCount", 0),
                "uptime": uptime, "image": (c.image.tags or ["?"])[0]}
    except Exception as e:
        return {"exists": True, "status": "error", "detail": str(e)[:120]}


def mcp_probe(port, timeout=12):
    """POST initialize to /mcp; then tools/list if a session is issued."""
    url = f"http://host.docker.internal:{port}/mcp"
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "mcp-monitor", "version": "1.0"}}}
    t0 = time.time()
    try:
        r = requests.post(url, json=init, headers=headers, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        body = r.text or ""
        sess = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
        tools = None
        # Try tools/list when server issued a session (or statelessly).
        try:
            h2 = dict(headers)
            if sess:
                h2["Mcp-Session-Id"] = sess
            r2 = requests.post(url, headers=h2, timeout=timeout,
                               json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            m = re.search(r'"tools"\s*:\s*\[(.*?)\]\s*,\s*"[^"]*"\s*:', r2.text, re.S)
            names = re.findall(r'"name"\s*:\s*"([^"]+)"', r2.text)
            if names:
                tools = sorted(set(names))
        except Exception:
            pass
        ok = r.status_code < 500 and ("result" in body or "capabilities" in body
                                      or "protocolVersion" in body or tools)
        return {"ok": bool(ok), "ms": ms, "http": r.status_code, "tools": tools,
                "sessions": bool(sess),
                "detail": ("" if ok else body[:200])}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "http": None, "tools": None, "detail": str(e)[:200]}


def log_progress(name, tail=2000):
    """Last progress-like line + percent from container logs."""
    try:
        c = dclient().containers.get(name)
        raw = c.logs(tail=tail).decode("utf-8", "replace")
    except Exception as e:
        return {"line": "", "percent": None, "license": "?", "detail": str(e)[:120]}
    lic = "ok" if ("validated" in raw or "License key provided" in raw) else \
          ("BAD" if ("Invalid" in raw and "LICENSE" in raw) else "?")
    percent, line = None, ""
    for ln in reversed(raw.splitlines()):
        m = PROGRESS_RE.search(ln)
        if m:
            try:
                done, total = int(m.group(2)), int(m.group(3))
                if total > 0:
                    percent = round(done * 100 / total)
            except Exception:
                pass
            eta = ETA_RE.search(ln)
            line = ln.strip()[-220:] + (f"  [ETA {eta.group(1)}]" if eta and "ETA" not in ln[-30:] else "")
            break
    if not line:
        interesting = [ln.strip()[-220:] for ln in raw.splitlines()
                       if re.search(r"index|ready|Ready|health|started|Starting|ERROR|error|model", ln)]
        line = interesting[-1] if interesting else ""
    return {"line": line, "percent": percent, "license": lic}


@app.get("/api/status")
def api_status():
    out = []
    for s in SERVERS:
        ci = container_info(s["container"])
        running = ci.get("status") == "running"
        mcp = mcp_probe(s["port"]) if (running and not s.get("no_mcp")) else \
            ({"ok": None, "detail": "container not running"} if not s.get("no_mcp")
             else {"ok": None, "detail": "no MCP here"})
        prog = log_progress(s["container"]) if ci.get("exists") else \
            {"line": "", "percent": None, "license": "?"}
        # Плохой ключ убивает контейнер за секунды: живой MCP = лицензия принята.
        # (stderr с текстом лицензии Docker SDK не отдаёт, смотрим по факту.)
        if prog.get("license") != "BAD" and mcp.get("ok"):
            prog["license"] = "ok"
        if running and ci.get("health") == "healthy":
            state = "ready" if (mcp.get("ok") or s.get("no_mcp")) else "degraded"
        elif running:
            state = "starting" if mcp.get("ok") is None else ("ready" if mcp.get("ok") else "indexing")
            if prog.get("percent") is not None:
                state = "indexing"
        elif ci.get("status") in ("restarting", "created"):
            state = "error"
        elif not ci.get("exists"):
            state = "missing"
        else:
            state = "stopped"
        out.append({"key": s["key"], "name": s["name"], "port": s["port"],
                    "desc": s["desc"], "expected_tools": s["tools"],
                    "state": state, "container": ci, "mcp": mcp, "progress": prog})
    return jsonify({"servers": out, "ts": int(time.time())})


@app.get("/api/logs/<name>")
def api_logs(name):
    tail = min(int(request.args.get("tail", "60")), 500)
    try:
        c = dclient().containers.get(name)
        return Response(c.logs(tail=tail).decode("utf-8", "replace"),
                        mimetype="text/plain")
    except Exception as e:
        return Response(str(e), status=404, mimetype="text/plain")


@app.post("/api/control/<name>/<action>")
def api_control(name, action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "bad action"}), 400
    try:
        c = dclient().containers.get(name)
        getattr(c, action)()
        return jsonify({"ok": True, "action": action, "name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>1C MCP — мониторинг</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e8ecf4;margin:0;padding:16px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#8b93a7;font-size:13px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px}
.card{background:#1a2133;border:1px solid #2a3450;border-radius:10px;padding:12px 14px}
.card h2{font-size:16px;margin:0 0 2px}.desc{color:#8b93a7;font-size:12px;margin-bottom:8px}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}
.ready{background:#0f5132;color:#7dffa8}.indexing{background:#5a4100;color:#ffd76a}
.starting{background:#0b3d5c;color:#7cc7ff}.stopped{background:#3a3f4d;color:#b9bfd0}
.error{background:#5c1010;color:#ff9a9a}.missing{background:#3a3f4d;color:#b9bfd0}.degraded{background:#5c2f0b;color:#ffb37c}
.row{font-size:13px;margin:3px 0}.row b{color:#aeb8d0;font-weight:600}
.bar{height:8px;background:#2a3450;border-radius:4px;margin:6px 0;overflow:hidden}
.bar i{display:block;height:100%;background:#4da3ff}
.logline{font-size:12px;color:#9fe8b4;background:#0c1220;border-radius:6px;padding:6px 8px;margin-top:6px;
white-space:pre-wrap;word-break:break-word;max-height:76px;overflow:auto}
.btns{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
button{background:#2a3450;color:#e8ecf4;border:1px solid #3a4670;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
button:hover{background:#35436b}
pre#biglog{background:#0c1220;border:1px solid #2a3450;border-radius:8px;padding:10px;max-height:300px;overflow:auto;font-size:12px}
#ts{color:#8b93a7;font-size:12px}
</style></head><body>
<h1>1C MCP — мониторинг флота</h1>
<div class="sub">Docker + MCP-зонды + прогресс индексации из логов. Обновление каждые 5 с. <span id="ts"></span></div>
<div class="grid" id="grid"></div>
<h3>Логи контейнера</h3>
<div class="btns" id="logbtns"></div>
<pre id="biglog">выберите контейнер…</pre>
<script>
const grid=document.getElementById('grid'),ts=document.getElementById('ts');
async function refresh(){
  const r=await fetch('/api/status');const j=await r.json();
  ts.textContent='обновлено '+new Date(j.ts*1000).toLocaleTimeString();
  grid.innerHTML=j.servers.map(s=>{
    const c=s.container,m=s.mcp,p=s.progress;
    const tools=(m.tools&&m.tools.length)?m.tools.join(', '):(s.expected_tools.join(', ')||'—');
    const bar=(p.percent!=null)?`<div class="bar"><i style="width:${p.percent}%"></i></div><div class="row">Индексация: <b>${p.percent}%</b></div>`:'';
    return `<div class="card"><h2>${s.name} <span style="color:#8b93a7">:${s.port}</span></h2>
    <div class="desc">${s.desc}</div>
    <div><span class="badge ${s.state}">${s.state}</span></div>
    <div class="row">Контейнер: <b>${c.status||'?'}</b>${c.health?' · health: <b>'+c.health+'</b>':''}${c.uptime?' · uptime '+c.uptime:''}${c.restarts?' · рестартов: <b>'+c.restarts+'</b>':''}</div>
    <div class="row">MCP: <b>${m.ok===true?'отвечает ('+m.ms+' мс)':(m.ok===false?'не отвечает':'—')}</b>${m.http?' · HTTP '+m.http:''} · лицензия: <b>${p.license}</b></div>
    <div class="row">Инструменты: ${tools}</div>${bar}
    ${p.line?`<div class="logline">${p.line.replace(/</g,'&lt;')}</div>`:''}
    <div class="btns"><button onclick="logs('${s.key}','${s.name}')">логи</button>
    <button onclick="ctl('${s.key}','start')">start</button><button onclick="ctl('${s.key}','stop')">stop</button>
    <button onclick="ctl('${s.key}','restart')">restart</button></div></div>`}).join('');
  const lb=document.getElementById('logbtns');
  if(!lb.children.length){lb.innerHTML=j.servers.map(s=>`<button onclick="logs('${s.key}','${s.name}')">${s.name}</button>`).join('');}
}
const CNAME={help:'1c_help_mcp',graph:'1c_graph_metadata',codemeta:'1c_code_metadata_mcp',ssl:'1c_ssl_mcp',templates:'1c_templates_mcp',syntax:'1c_syntaxcheck_mcp',checker:'1c_code_checker',neo4j:'neo4j'};
async function ctl(key,action){
  await fetch(`/api/control/${CNAME[key]}/${action}`,{method:'POST'});refresh();
}
async function logs(key,title){
  const cname=CNAME[key]||key;
  const r=await fetch(`/api/logs/${cname}?tail=80`);
  document.getElementById('biglog').textContent='=== '+cname+' ===\\n'+await r.text();
}
refresh();setInterval(refresh,5000);
</script></body></html>"""


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
