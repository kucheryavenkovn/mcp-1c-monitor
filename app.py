"""1C MCP fleet monitor: Docker state + MCP liveness + index progress + GPU panel
с переключением серверов GPU/CPU. Single file."""
import json
import os
import re
import time
from datetime import datetime, timezone

import docker
import requests
from flask import Flask, jsonify, request, Response

app = Flask(__name__)
ENV = os.environ.get

# Stable fleet. `container=None` means compose-managed pair shown separately.
SERVERS = [
    {"key": "help", "name": "HelpSearch", "container": "1c_help_mcp",
     "port": 8003, "desc": "Справка платформы 1С", "tools": ["docsearch", "docinfo", "formatspec", "standards"],
     "channel": "stable · latest"},
    {"key": "graph", "name": "GraphMetadata", "container": "1c_graph_metadata",
     "port": 8006, "desc": "Граф связей (Neo4j)", "tools": ["list_graph_projects", "resolve_effective_entity"],
     "channel": "stable · контракт 1.x (СТАРЫЙ)",
     "note": "Не работает с Designer XML напрямую (ждёт .txt старого формата). Контейнеры удалены; рабочий — beta-контур ниже."},
    {"key": "codemeta", "name": "CodeMetadata", "container": "1c_code_metadata_mcp",
     "port": 8000, "desc": "Метаданные + BSL-код (28 инструментов)", "tools": ["metadatasearch", "codesearch", "stats"],
     "channel": "stable · latest"},
    {"key": "ssl", "name": "SSLSearch", "container": "1c_ssl_mcp",
     "port": 8008, "desc": "Поиск по БСП", "tools": ["ssl_search"],
     "channel": "stable · latest"},
    {"key": "templates", "name": "TemplatesSearch", "container": "1c_templates_mcp",
     "port": 8004, "desc": "Шаблоны + память проекта", "tools": ["templatesearch", "remember", "recall"],
     "channel": "stable · latest"},
    {"key": "syntax", "name": "SyntaxCheck", "container": "1c_syntaxcheck_mcp",
     "port": 8002, "desc": "Проверка синтаксиса BSL", "tools": ["syntaxcheck"],
     "channel": "stable · latest"},
    {"key": "checker", "name": "1CCodeChecker", "container": "1c_code_checker",
     "port": 8007, "desc": "Проверка через 1С:Напарник (нужен токен)", "tools": [],
     "channel": "stable · latest"},
    {"key": "neo4j", "name": "Neo4j (для Graph)", "container": "neo4j",
     "port": 7474, "desc": "Графовая БД", "tools": [], "no_mcp": True,
     "channel": "stable-контур (СТАРЫЙ)",
     "note": "База нерабочего stable-контура. Контейнер удалён."},
    {"key": "graphbeta", "name": "GraphMetadata-beta", "container": "1c_graph_metadata_beta",
     "port": 8106, "desc": "Граф связей, beta-контур (Neo4j-beta)", "tools": ["list_graph_projects", "resolve_effective_entity"],
     "channel": "beta · контракт 2.0 (РАБОЧИЙ)",
     "note": "Единственный рабочий граф: читает Designer XML и расширения напрямую."},
    {"key": "neo4jbeta", "name": "Neo4j-beta", "container": "neo4j_beta",
     "port": 7574, "desc": "Графовая БД beta-контура", "tools": [], "no_mcp": True,
     "channel": "beta-контур (РАБОЧИЙ)"},
]

PROGRESS_RE = re.compile(
    r"([\w\s]{3,40}?progress)\s*:\s*(\d+)\s*/\s*(\d+)[^|\n]*?(?:\|\s*([^\n|]*?))?\s*(?:\|\s*ETA\s*([^\n|]+))?",
    re.IGNORECASE)
ETA_RE = re.compile(r"ETA\s*([^\s|]+)", re.IGNORECASE)

# Спецификации запуска GPU-серверов (latest-образы со встроенной моделью).
# Секретов здесь НЕТ: ключи и пути берутся из окружения монитора.
# Binds: (ENV-ИМЯ [или (ENV-ИМЯ, подкаталог)], путь в контейнере, режим).
RUN_SPECS = {
    "help": {"container": "1c_help_mcp", "image": "comol/1c_help_mcp:latest",
             "lic_env": "LICENSE_KEY_HELP", "port": 8003,
             "env": {"1C_BIN_PATH": "/1c_docs", "RESET_CACHE": "false",
                     "RESET_DATABASE": "false", "USESSE": "false"},
             "binds": [("PATH_1C_BIN", "/1c_docs", "rw"),
                       (("PATH_BASES", "mcp_docs"), "/app/chroma_db", "rw")]},
    "ssl": {"container": "1c_ssl_mcp", "image": "comol/mcp_ssl_server:latest",
            "lic_env": "LICENSE_KEY_SSL", "port": 8008,
            "env": {"SSL_VERSION": ("ENV", "SSL_VERSION"), "RESET_DATABASE": "false",
                    "USESSE": "false"},
            "binds": [(("PATH_BASES", "mcp_ssl"), "/app/zvec_db", "rw")]},
    "templates": {"container": "1c_templates_mcp", "image": "comol/template-search-mcp:latest",
                  "lic_env": "LICENSE_KEY_TEMPLATES", "port": 8004,
                  "env": {"RESET_CACHE": "false", "RESET_DATABASE": "false",
                          "USESSE": "false"},
                  "binds": [(("PATH_BASES", "mcp_templates"), "/app/chroma_db", "rw")]},
    "codemeta": {"container": "1c_code_metadata_mcp", "image": "comol/1c_code_metadata_mcp:latest",
                 "lic_env": "LICENSE_KEY_CODEMETADATA", "port": 8000,
                 "env": {"METADATA_PATH": "/app/code", "CODE_PATH": "/app/code",
                         "SOURCE_FORMAT": "auto", "RESET_CACHE": "false",
                         "RESET_DATABASE": "false", "USESSE": "false"},
                 "binds": [("PATH_CODE", "/app/code", "ro"),
                           (("PATH_BASES", "mcp_codemetadata"), "/app/chroma_db", "rw")]},
}


def resolve_bind(expr):
    if isinstance(expr, tuple):
        base, sub = expr
        return f"{ENV(base, '')}/{sub}"
    return ENV(expr, "")


def recreate(key, use_gpu):
    """Пересоздать контейнер сервера с GPU или без. Индексы живут в томах."""
    spec = RUN_SPECS[key]
    lic = ENV(spec["lic_env"], "")
    if not lic:
        return {"ok": False, "error": f"в окружении монитора нет {spec['lic_env']}"}
    env = {"LICENSE_KEY": lic}
    for k, v in spec["env"].items():
        env[k] = ENV(v[1], "") if isinstance(v, tuple) else v
    volumes, missing = {}, []
    for src_expr, dst, mode in spec["binds"]:
        # Пути Windows-хоста: внутри Linux-контейнера их не проверить,
        # непустой строки достаточно — демон Docker сам сообщит об ошибке.
        host = resolve_bind(src_expr).rstrip("/")
        if not host:
            missing.append(str(src_expr))
            continue
        volumes[host] = {"bind": dst, "mode": mode}
    if missing:
        return {"ok": False, "error": f"нет путей на хосте: {missing}"}
    client = dclient()
    try:
        old = client.containers.get(spec["container"])
        old.stop(timeout=30)
        old.remove()
    except docker.errors.NotFound:
        pass
    kwargs = {"image": spec["image"], "name": spec["container"], "detach": True,
              "environment": env, "volumes": volumes,
              "ports": {f"{spec['port']}/tcp": ("127.0.0.1", spec["port"])},
              "restart_policy": {"Name": "unless-stopped"}}
    if use_gpu:
        from docker.types import DeviceRequest
        kwargs["device_requests"] = [DeviceRequest(count=-1, capabilities=[["gpu"]])]
    c = client.containers.run(**kwargs)
    return {"ok": True, "id": c.short_id, "mode": "GPU" if use_gpu else "CPU"}


def gpu_summary():
    """VRAM и процессы через NVML. Нужен запуск монитора с --gpus all."""
    try:
        import pynvml as nv
        nv.nvmlInit()
        h = nv.nvmlDeviceGetHandleByIndex(0)
        mem = nv.nvmlDeviceGetMemoryInfo(h)
        try:
            name = nv.nvmlDeviceGetName(h)
            name = name.decode() if isinstance(name, bytes) else str(name)
        except Exception:
            name = "GPU-0"
        procs = []
        try:
            for p in nv.nvmlDeviceGetComputeRunningProcesses(h):
                try:
                    pn = nv.nvmlSystemGetProcessName(p.pid)
                    pn = pn.decode() if isinstance(pn, bytes) else str(pn)
                except Exception:
                    pn = "?"
                procs.append({"pid": p.pid, "name": pn.split("/")[-1][:40],
                              "mem_mb": round(p.usedGpuMemory / 1048576)})
        except Exception:
            pass
        procs.sort(key=lambda x: -x["mem_mb"])
        return {"available": True, "name": name,
                "total_mb": round(mem.total / 1048576),
                "used_mb": round(mem.used / 1048576),
                "free_mb": round(mem.free / 1048576), "procs": procs[:20]}
    except Exception as e:
        return {"available": False,
                "detail": str(e)[:200] + " (запустите монитор с --gpus all)"}


def parse_mcp_payloads(text):
    """SSE data: строки и plain JSON -> список payload."""
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln.startswith("data:"):
            ln = ln[5:].strip()
        if ln.startswith("{"):
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def mcp_call(port, tool, args, timeout=90):
    """Вызов MCP-инструмента: initialize -> tools/call. Возвращает текст результата."""
    r = mcp_result(port, tool, args, timeout)
    return r["text"]


def mcp_result(port, tool, args, timeout=90):
    """То же, но сырым конвертом результата (dict)."""
    url = f"http://host.docker.internal:{port}/mcp"
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    base = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "mcp-monitor", "version": "1.0"}}}
    try:
        s = requests.Session()
        r = s.post(url, headers=h, timeout=timeout, json=base)
        sess = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
        h2 = dict(h)
        if sess:
            h2["Mcp-Session-Id"] = sess
        try:
            s.post(url, headers=h2, timeout=15,
                   json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        r2 = s.post(url, headers=h2, timeout=timeout,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": tool, "arguments": args or {}}})
        texts, data = [], None
        for p in parse_mcp_payloads(r2.text):
            res = p.get("result", {})
            if res:
                data = res
            for c in res.get("content", []) or []:
                if isinstance(c, dict) and c.get("text"):
                    texts.append(str(c["text"]))
            if res.get("structuredContent") and not texts:
                texts.append(json.dumps(res["structuredContent"],
                                        ensure_ascii=False)[:4000])
            if p.get("error") and not texts:
                texts.append("ERROR: " + json.dumps(p["error"], ensure_ascii=False)[:500])
        # Контракт 2.0 графа: content.text — сам JSON-конверт (total/cursor/items).
        # Распаковываем, иначе regex бежит по строке без настоящих переводов строк.
        if texts:
            try:
                inner = json.loads(texts[0])
                if isinstance(inner, dict) and ("text" in inner or "items" in inner):
                    data = inner
                    if isinstance(inner.get("text"), str):
                        texts[0] = inner["text"]
            except Exception:
                pass
        body = "\n".join(texts) if texts else r2.text[:2000]
        return {"ok": True, "text": body[:4000], "data": data}
    except Exception as e:
        return {"ok": False, "text": str(e)[:300], "data": None}


def du_mb(container, path):
    try:
        c = dclient().containers.get(container)
        out = c.exec_run(["du", "-sb", path])
        num = out.output.decode("utf-8", "replace").split()[0]
        return round(int(num) / 1048576, 1)
    except Exception:
        return None


# Что показывать в панели «Данные»: MCP-запросы, замеры диска, действия.
DATA_SPECS = {
    "codemeta": {"port": 8000,
                 "calls": [("stats", {})],
                 "du": [("1c_code_metadata_mcp", "/app/chroma_db")],
                 "actions": ["reindex"]},
    "graphbeta": {"port": 8106,
                  "calls": [("get_graph_stats", {}), ("list_graph_projects", {}),
                            ("get_indexing_status", {})],
                  "du": [("1c_graph_metadata_beta", "/app/data"),
                         ("neo4j_beta", "/data")],
                  "actions": ["refresh_layers", "delete_project"]},
    "help": {"port": 8003, "calls": [],
             "du": [("1c_help_mcp", "/app/chroma_db")], "actions": []},
    "ssl": {"port": 8008, "calls": [],
            "du": [("1c_ssl_mcp", "/app/zvec_db")], "actions": []},
    "templates": {"port": 8004, "calls": [],
                  "du": [("1c_templates_mcp", "/app/chroma_db")], "actions": []},
}


@app.get("/api/data/<key>")
def api_data(key):
    spec = DATA_SPECS.get(key)
    if not spec:
        return jsonify({"error": "no data spec"}), 404
    stats = []
    for tool, args in spec["calls"]:
        r = mcp_call(spec["port"], tool, args)
        stats.append({"tool": tool, "ok": r["ok"], "text": r["text"]})
    disk = [{"path": f"{c}:{p}", "mb": du_mb(c, p)} for c, p in spec["du"]]
    return jsonify({"stats": stats, "disk": disk, "actions": spec["actions"]})


@app.post("/api/action/<key>/<action>")
def api_action(key, action):
    import uuid as _uuid
    if key == "codemeta" and action == "reindex":
        return jsonify(mcp_call(8000, "reindex", {}, timeout=60))
    if key == "graphbeta" and action == "refresh_layers":
        op = _uuid.uuid4().hex[:12]
        return jsonify(mcp_call(8106, "refresh_extension_layers",
                                {"operation_id": op}, timeout=600))
    if key == "graphbeta" and action == "delete_project":
        pid = (request.get_json(silent=True) or {}).get("project_id", "")
        if not pid:
            return jsonify({"ok": False, "text": "нужен project_id"}), 400
        return jsonify(mcp_call(8106, "delete_graph_project",
                                {"project_id": pid,
                                 "operation_id": _uuid.uuid4().hex[:12]},
                                timeout=120))
    return jsonify({"ok": False, "text": "unknown action"}), 400


# --- Просмотрщик метаданных графа (base/слои расширений) ---
META_CATS = ["Справочники", "Документы", "РегистрыСведений",
             "РегистрыНакопления", "Перечисления", "Отчеты", "Обработки",
             "Константы", "ОбщиеМодули", "Роли", "ПодпискиНаСобытия",
             "БизнесПроцессы", "Задачи", "ЖурналыДокументов"]
OBJ_RE = re.compile(r"object_name:\s*(.+?)\s*$", re.M)


def g_template(op, params, extra=None):
    q = json.dumps({"operation": op, **params}, ensure_ascii=False)
    args = {"query": q}
    if extra:
        args.update(extra)
    return mcp_result(8106, "search_metadata", args, timeout=120)


@app.get("/api/meta/cats")
def api_meta_cats():
    out = []
    for c in META_CATS:
        r = g_template("list_objects_by_category", {"category_name": c},
                       {"max_items": 1})
        total = (r.get("data") or {}).get("total", 0) if r.get("ok") else 0
        out.append({"name": c, "total": total})
    return jsonify({"cats": out})


@app.get("/api/meta/objects")
def api_meta_objects():
    cat = request.args.get("category", "")
    q = request.args.get("q", "")
    cursor = request.args.get("cursor", "")
    limit = min(int(request.args.get("limit", "20")), 50)
    if q:
        params = {"object_name": q}
        if cat:
            params["category"] = cat
        r = g_template("list_objects_by_name", params,
                       {"max_items": limit, **({"cursor": cursor} if cursor else {})})
    else:
        r = g_template("list_objects_by_category", {"category_name": cat},
                       {"max_items": limit, **({"cursor": cursor} if cursor else {})})
    data = r.get("data") or {}
    names = OBJ_RE.findall(r.get("text", ""))
    return jsonify({"ok": r.get("ok"), "names": names,
                    "total": data.get("total", len(names)),
                    "cursor": data.get("cursor", "")})


@app.get("/api/meta/object")
def api_meta_object():
    name = request.args.get("name", "")
    if not name:
        return jsonify({"error": "need name"}), 400
    struct = g_template("object_structure", {"object_name": name})
    eff = mcp_result(8106, "resolve_effective_entity", {"object_name": name})
    layers, effective, extending = [], {}, []
    try:
        item = (eff.get("data") or {}).get("items", [])[0]
        effective = item.get("effective", {}) or {}
        extending = item.get("extending", []) or []
        layers = ((eff.get("data") or {}).get("data", {}) or {}).get("layers", []) or []
    except Exception:
        pass
    forms = g_template("list_forms", {"object_name": name})
    return jsonify({
        "name": name,
        "structure": struct.get("text", "")[:3000],
        "effective": {"layer": effective.get("layer", ""),
                      "origin": effective.get("origin", "")},
        "extending": [{"layer": e.get("layer", ""), "relation": e.get("relation", ""),
                       "origin": e.get("origin", "")} for e in extending],
        "layers": [{"layer": l.get("layer", ""), "kind": l.get("kind", "")} for l in layers],
        "forms": forms.get("text", "")[:1200]})


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
        # Флаг GPU: контейнер создан с device request на видеокарту.
        gpu = False
        try:
            for d in (c.attrs.get("HostConfig", {}) or {}).get("DeviceRequests") or []:
                caps = str(d.get("Capabilities", [])).lower()
                if "gpu" in caps or (d.get("Count", 0) or 0) != 0:
                    gpu = True
        except Exception:
            pass
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
                "health": health, "gpu": gpu, "restarts": st.get("RestartCount", 0),
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
                    "channel": s.get("channel", ""), "note": s.get("note", ""),
                    "switchable": s["key"] in RUN_SPECS,
                    "has_data": s["key"] in DATA_SPECS,
                    "state": state, "container": ci, "mcp": mcp, "progress": prog})
    return jsonify({"servers": out, "ts": int(time.time())})


@app.get("/api/gpu")
def api_gpu():
    return jsonify(gpu_summary())


@app.post("/api/mode/<key>/<which>")
def api_mode(key, which):
    if key not in RUN_SPECS or which not in ("gpu", "cpu"):
        return jsonify({"ok": False, "error": "bad key/mode"}), 400
    try:
        return jsonify(recreate(key, which == "gpu"))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


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
.chan{font-size:11px;color:#7cc7ff;margin-bottom:6px}.note{font-size:12px;color:#ffd76a;margin:6px 0}
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
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.65);display:flex;align-items:center;justify-content:center;z-index:50}
.modal{background:#1a2133;border:1px solid #3a4670;border-radius:12px;padding:16px;max-width:820px;width:92%;max-height:86vh;overflow:auto}
.modal h3{margin:0 0 8px;font-size:16px}
#ts{color:#8b93a7;font-size:12px}
</style></head><body>
<h1>1C MCP — мониторинг флота</h1>
<div class="sub">Docker + MCP-зонды + прогресс индексации из логов. Обновление каждые 5 с. <span id="ts"></span><br>
<a href="/metadata" style="color:#7cc7ff">Просмотрщик метаданных базы и расширения →</a><br>
<label><input type="checkbox" id="showdead"> показывать остановленные / legacy</label> <span id="hidden"></span></div>
<h3>Видеокарта</h3>
<div class="card" id="gpupanel">опрос NVML…</div>
<h3>Серверы</h3>
<div class="grid" id="grid"></div>
<h3>Логи контейнера</h3>
<div class="btns" id="logbtns"></div>
<pre id="biglog">выберите контейнер…</pre>
<div class="overlay" id="ovl" style="display:none" onclick="if(event.target===this)closeModal()">
<div class="modal"><div class="btns" style="margin:0 0 8px"><button onclick="closeModal()">закрыть ✕</button> <span id="modaltitle"></span></div><div id="modalbody">…</div></div>
</div>
<script>
const grid=document.getElementById('grid'),ts=document.getElementById('ts');
const showBox=document.getElementById('showdead'),hiddenBox=document.getElementById('hidden');
showBox.checked=localStorage.getItem('mcpmon_showdead')==='1';
showBox.onchange=()=>{localStorage.setItem('mcpmon_showdead',showBox.checked?'1':'0');refresh();};
async function refresh(){
  try{
    await gpuRefresh();
    const r=await fetch('/api/status');
    if(!r.ok)throw new Error('HTTP '+r.status);
    const j=await r.json();
  ts.textContent='обновлено '+new Date(j.ts*1000).toLocaleTimeString();
  const vis=j.servers.filter(s=>showBox.checked||!['stopped','missing'].includes(s.state));
  hiddenBox.textContent=showBox.checked?'':('скрыто остановленных: '+(j.servers.length-vis.length));
  grid.innerHTML=vis.map(s=>{
    const c=s.container,m=s.mcp,p=s.progress;
    const tools=(m.tools&&m.tools.length)?m.tools.join(', '):(s.expected_tools.join(', ')||'—');
    const bar=(p.percent!=null)?`<div class="bar"><i style="width:${p.percent}%"></i></div><div class="row">Индексация: <b>${p.percent}%</b></div>`:'';
    return `<div class="card"><h2>${s.name} <span style="color:#8b93a7">:${s.port}</span></h2>
    <div class="desc">${s.desc}</div>
    ${s.channel?`<div class="chan">${s.channel}</div>`:''}
    ${s.note?`<div class="note">${s.note}</div>`:''}
    <div><span class="badge ${s.state}">${s.state}</span></div>
    <div class="row">Контейнер: <b>${c.status||'?'}</b>${c.health?' · health: <b>'+c.health+'</b>':''}${c.uptime?' · uptime '+c.uptime:''}${c.restarts?' · рестартов: <b>'+c.restarts+'</b>':''}</div>
    <div class="row">MCP: <b>${m.ok===true?'отвечает ('+m.ms+' мс)':(m.ok===false?'не отвечает':'—')}</b>${m.http?' · HTTP '+m.http:''} · лицензия: <b>${p.license}</b></div>
    ${s.switchable?`<div class="row">Режим: <b>${c.gpu?'GPU':'CPU'}</b></div>`:''}
    <div class="row">Инструменты: ${tools}</div>${bar}
    ${p.line?`<div class="logline">${p.line.replace(/</g,'&lt;')}</div>`:''}
    <div class="btns"><button onclick="logs('${s.key}','${s.name}')">логи</button>
    ${s.has_data?`<button onclick="dataPanel('${s.key}','${s.name}')">данные</button>`:''}
    ${s.switchable?`<button onclick="mode('${s.key}','gpu')">на GPU</button><button onclick="mode('${s.key}','cpu')">на CPU</button>`:''}
    <button onclick="ctl('${s.key}','start')">start</button><button onclick="ctl('${s.key}','stop')">stop</button>
    <button onclick="ctl('${s.key}','restart')">restart</button></div></div>`}).join('');
  const lb=document.getElementById('logbtns');
  if(!lb.children.length){lb.innerHTML=j.servers.map(s=>`<button onclick="logs('${s.key}','${s.name}')">${s.name}</button>`).join('');}
  }catch(e){
    document.getElementById('grid').innerHTML=`<div class="card"><div class="row">Нет связи с монитором (${String(e).slice(0,100)}). Проверьте контейнер mcp_1c_monitor, страница повторит сама.</div></div>`;
  }
}
const CNAME={help:'1c_help_mcp',graph:'1c_graph_metadata',codemeta:'1c_code_metadata_mcp',ssl:'1c_ssl_mcp',templates:'1c_templates_mcp',syntax:'1c_syntaxcheck_mcp',checker:'1c_code_checker',neo4j:'neo4j',graphbeta:'1c_graph_metadata_beta',neo4jbeta:'neo4j_beta'};
async function ctl(key,action){
  await fetch(`/api/control/${CNAME[key]}/${action}`,{method:'POST'});refresh();
}
async function mode(key,which){
  if(!confirm(`Пересоздать сервер в режиме ${which.toUpperCase()}? Индексы сохранятся (тома), займёт ~1 мин.`))return;
  const r=await fetch(`/api/mode/${key}/${which}`,{method:'POST'});
  const j=await r.json();
  if(!j.ok)alert('Ошибка: '+(j.error||'unknown'));
  setTimeout(refresh,10000);
}
async function logs(key,title){
  const cname=CNAME[key]||key;
  const r=await fetch(`/api/logs/${cname}?tail=80`);
  document.getElementById('biglog').textContent='=== '+cname+' ===\\n'+await r.text();
}
function closeModal(){document.getElementById('ovl').style.display='none';}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});
async function dataPanel(key,title){
  document.getElementById('ovl').style.display='flex';
  document.getElementById('modaltitle').textContent='Данные: '+(title||key);
  loadData(key);
}
async function loadData(key){
  const box=document.getElementById('modalbody');
  box.innerHTML='запрос stats…';
  const d=await (await fetch(`/api/data/${key}`)).json();
  let html='';
  (d.disk||[]).forEach(x=>{html+=`<div class="row">Диск ${x.path.replace(/</g,'&lt;')}: <b>${x.mb??'?'} МБ</b></div>`;});
  (d.stats||[]).forEach(s=>{html+=`<div class="row">§ ${s.tool} ${s.ok?'':'(ошибка)'}</div><div class="logline" style="max-height:150px">${s.text.replace(/</g,'&lt;')}</div>`;});
  const acts={"reindex":"переиндексировать (фон)","refresh_layers":"перечитать слои расширений (долго!)","delete_project":"удалить граф-проект…"};
  (d.actions||[]).forEach(a=>{html+=`<button onclick="runAction('${key}','${a}')">${acts[a]||a}</button> `;});
  box.innerHTML=html||'нет данных';
}
async function runAction(key,action){
  let body=null;
  if(action==='delete_project'){
    const pid=prompt('project_id для удаления (см. list_graph_projects выше):');
    if(!pid)return;
    if(!confirm(`УДАЛИТЬ проект ${pid} из графа? Безвозвратно.`))return;
    body={project_id:pid};
  }else if(!confirm('Выполнить «'+action+'»?'))return;
  const r=await fetch(`/api/action/${key}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):null});
  const j=await r.json();
  alert((j.ok?'OK: ':'ОШИБКА: ')+(j.text||j.error||'').slice(0,500));
  loadData(key);
}
async function gpuRefresh(){
  const box=document.getElementById('gpupanel');
  try{
    const g=await (await fetch('/api/gpu')).json();
    if(!g.available){box.innerHTML='NVML недоступен: '+(g.detail||'').replace(/</g,'&lt;');return;}
    const pct=Math.round(g.used_mb*100/Math.max(g.total_mb,1));
    const rows=(g.procs||[]).map(p=>`<div class="row">PID ${p.pid} · ${p.name.replace(/</g,'&lt;')} · <b>${p.mem_mb} МБ</b></div>`).join('')||'<div class="row">процессов на GPU нет</div>';
    box.innerHTML=`<div class="row"><b>${g.name.replace(/</g,'&lt;')}</b> · занято <b>${g.used_mb}</b> / ${g.total_mb} МБ · свободно <b>${g.free_mb}</b> МБ</div>
    <div class="bar"><i style="width:${pct}%"></i></div>${rows}
    <div class="row">Контейнеры 1С с доступом к GPU помечены «Режим: GPU» на карточках. Переключение — кнопки «на GPU»/«на CPU» (пересоздание, индексы в томах сохраняются).</div>`;
  }catch(e){box.innerHTML='ошибка опроса GPU';}
}
refresh();setInterval(refresh,5000);
</script></body></html>"""


@app.get("/")
def index():
    # Без кеша: иначе вкладка, открытая во время пересборки, висит с битой версией.
    return Response(PAGE, mimetype="text/html",
                    headers={"Cache-Control": "no-store, max-age=0"})


META_PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Метаданные конфигурации — просмотрщик</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e8ecf4;margin:0;padding:16px}
a{color:#7cc7ff}.chips{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.chip{background:#2a3450;border:1px solid #3a4670;border-radius:16px;padding:4px 12px;font-size:12px;cursor:pointer}
.chip.on{background:#0b3d5c;border-color:#4da3ff}
.chip small{color:#8b93a7}
.cols{display:grid;grid-template-columns:minmax(280px,380px) 1fr;gap:12px}
.panel{background:#1a2133;border:1px solid #2a3450;border-radius:10px;padding:12px}
.orow{padding:6px 8px;border-radius:6px;cursor:pointer;font-size:13px}
.orow:hover{background:#2a3450}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}
.base{background:#0f5132;color:#7dffa8}.ext{background:#5a4100;color:#ffd76a}
pre{background:#0c1220;border-radius:6px;padding:8px;font-size:12px;max-height:300px;overflow:auto;white-space:pre-wrap}
input[type=text]{background:#0c1220;color:#e8ecf4;border:1px solid #3a4670;border-radius:6px;padding:6px 10px;font-size:13px;width:280px}
button{background:#2a3450;color:#e8ecf4;border:1px solid #3a4670;border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer}
</style></head><body>
<h2>Метаданные: база и расширение</h2>
<div><a href="/">назад к мониторингу</a> · проект <b>1C Metadata Project</b> (beta-контур :8106)</div>
<div style="margin-top:8px"><input type="text" id="q" placeholder="поиск по имени…"> <button onclick="search()">найти</button></div>
<div class="chips" id="chips"></div>
<div class="cols"><div class="panel"><div id="count"></div><div id="list"></div><button id="more" style="display:none" onclick="more()">ещё</button></div>
<div class="panel" id="detail">выберите объект слева…</div></div>
<script>
let cur={cat:'',q:'',cursor:''};
async function cats(){
  const j=await (await fetch('/api/meta/cats')).json();
  document.getElementById('chips').innerHTML=j.cats.map(c=>
    `<span class="chip" id="chip-${c.name}" onclick="pick('${c.name}')">${c.name} <small>${c.total}</small></span>`).join('');
}
async function pick(cat){
  document.querySelectorAll('.chip').forEach(e=>e.classList.remove('on'));
  const el=document.getElementById('chip-'+cat);if(el)el.classList.add('on');
  cur={cat:cat,q:'',cursor:''};await load(true);
}
async function search(){cur={cat:'',q:document.getElementById('q').value,cursor:''};
  document.querySelectorAll('.chip').forEach(e=>e.classList.remove('on'));await load(true);}
async function load(fresh){
  const p=new URLSearchParams({category:cur.cat,q:cur.q,limit:20,cursor:fresh?'':cur.cursor});
  const j=await (await fetch('/api/meta/objects?'+p)).json();
  cur.cursor=j.cursor||'';
  document.getElementById('count').innerHTML='<b>'+j.total+'</b> объектов';
  const html=j.names.map(n=>`<div class="orow" data-n="${n.replace(/"/g,'&quot;')}">${n.replace(/</g,'&lt;')}</div>`).join('');
  const box=document.getElementById('list');
  box.innerHTML=fresh?html:box.innerHTML+html;
  box.querySelectorAll('.orow').forEach(e=>{e.onclick=()=>detail(e.getAttribute('data-n'));});
  document.getElementById('more').style.display=j.cursor?'':'none';
}
async function more(){await load(false);}
async function detail(name){
  const box=document.getElementById('detail');box.innerHTML='загрузка…';
  const j=await (await fetch('/api/meta/object?name='+encodeURIComponent(name))).json();
  const badge=o=>o==='base'?'<span class="badge base">БАЗА</span>':'<span class="badge ext">РАСШИРЕНИЕ</span>';
  let h='<h3>'+j.name.replace(/</g,'&lt;')+'</h3>';
  h+='<div>Действует: <b>'+(j.effective.layer||'').replace(/</g,'&lt;')+'</b> '+badge(j.effective.origin)+'</div>';
  (j.extending||[]).forEach(e=>{h+='<div>Слой: <b>'+e.layer.replace(/</g,'&lt;')+'</b> '+badge(e.origin)+' · '+e.relation+'</div>';});
  h+='<h4>Структура</h4><pre>'+j.structure.replace(/</g,'&lt;')+'</pre>';
  h+='<h4>Формы</h4><pre>'+j.forms.replace(/</g,'&lt;')+'</pre>';
  box.innerHTML=h;
}
cats();
</script></body></html>"""


@app.get("/metadata")
def metadata_page():
    return Response(META_PAGE, mimetype="text/html",
                    headers={"Cache-Control": "no-store, max-age=0"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
