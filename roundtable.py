#!/usr/bin/env python3
"""
Roundtable - talk with Claude, ChatGPT and Gemini in one shared conversation.

Run:   python roundtable.py
Then:  your browser opens http://127.0.0.1:8765

No dependencies beyond Python 3.8+. The server listens on 127.0.0.1 only.
API keys and saved conversations live in the "roundtable_data" folder next to
this file (or set ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY instead).
"""
import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ROUNDTABLE_DATA") or (APP_DIR / "roundtable_data"))
CONV_DIR = DATA_DIR / "conversations"
CONFIG_PATH = DATA_DIR / "config.json"

PROVIDERS = {
    "claude": {
        "name": "Claude",
        "env": ["ANTHROPIC_API_KEY"],
        "default_model": "claude-opus-5-5",
        "base": os.environ.get("ROUNDTABLE_ANTHROPIC_URL", "https://api.anthropic.com"),
    },
    "chatgpt": {
        "name": "ChatGPT",
        "env": ["OPENAI_API_KEY"],
        "default_model": "gpt-6.1-sol",
        "base": os.environ.get("ROUNDTABLE_OPENAI_URL", "https://api.openai.com"),
    },
    "gemini": {
        "name": "Gemini",
        "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "default_model": "gemini-4-argon",
        "base": os.environ.get("ROUNDTABLE_GEMINI_URL", "https://generativelanguage.googleapis.com"),
    },
}

CLAUDE_MAX_TOKENS = 16000   # includes the model's thinking, so leave headroom
REQUEST_TIMEOUT = 600       # seconds; reasoning models can take a while

_cfg_lock = threading.Lock()


class ProviderError(Exception):
    pass


# ----------------------------------------------------------------- config ---

def load_config():
    with _cfg_lock:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    cfg.setdefault("name", "Moderator")
    cfg.setdefault("keys", {})
    cfg.setdefault("models", {})
    return cfg


def save_config(cfg):
    with _cfg_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)


def get_key(cfg, provider):
    """Returns (key, source). A key saved in Settings wins over the environment."""
    k = (cfg["keys"].get(provider) or "").strip()
    if k:
        return k, "file"
    for var in PROVIDERS[provider]["env"]:
        k = (os.environ.get(var) or "").strip()
        if k:
            return k, "env"
    return "", None


def get_model(cfg, provider):
    return (cfg["models"].get(provider) or "").strip() or PROVIDERS[provider]["default_model"]


def public_config(cfg):
    out = {"name": cfg["name"], "workspace": cfg.get("workspace", ""), "providers": {}}
    for pid, p in PROVIDERS.items():
        _, source = get_key(cfg, pid)
        out["providers"][pid] = {
            "name": p["name"],
            "model": get_model(cfg, pid),
            "hasKey": source is not None,
            "keySource": source,
        }
    return out


# ------------------------------------------------------------- HTTP calls ---

def _error_text(raw):
    try:
        j = json.loads(raw)
    except ValueError:
        return raw.strip()[:500] or "no details"
    err = j.get("error", j) if isinstance(j, dict) else j
    if isinstance(err, dict):
        return str(err.get("message") or err)[:800]
    return str(err)[:800]


def http_json(url, headers, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "roundtable-local/1.0")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise ProviderError("HTTP %s: %s" % (e.code, _error_text(raw)))
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            reason += (" (Python can't verify the site's certificate. On macOS run "
                       "'Install Certificates.command' from your Python folder; on a managed "
                       "network you may need your organization's CA bundle via SSL_CERT_FILE.)")
        raise ProviderError("Could not reach the API: " + reason)
    except (socket.timeout, TimeoutError):
        raise ProviderError("Timed out after %d seconds waiting for a reply." % REQUEST_TIMEOUT)
    except ValueError:
        raise ProviderError("The API returned something that wasn't JSON.")


def claude_headers(key):
    h = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    ws = (load_config().get("workspace") or os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    if ws:
        h["anthropic-workspace-id"] = ws
    return h


def call_claude(key, model, system, messages):
    body = {
        "model": model,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": system,
        "messages": [{"role": m["role"], "content": m["text"]} for m in messages],
    }
    data = http_json(PROVIDERS["claude"]["base"] + "/v1/messages",
                     claude_headers(key), body)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text.strip():
        raise ProviderError("Claude returned no text (stop reason: %s)." % data.get("stop_reason"))
    if data.get("stop_reason") == "max_tokens":
        text += "\n\n*[Reply cut off at the output limit.]*"
    return text


def call_chatgpt(key, model, system, messages):
    body = {
        "model": model,
        "instructions": system,
        "input": [{"role": m["role"], "content": m["text"]} for m in messages],
        "store": False,
    }
    data = http_json(PROVIDERS["chatgpt"]["base"] + "/v1/responses",
                     {"Authorization": "Bearer " + key}, body)
    parts = []
    for item in data.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "refusal":
                parts.append(c.get("refusal", ""))
    text = "".join(parts)
    if not text.strip():
        detail = (data.get("incomplete_details") or {}).get("reason") or data.get("status")
        raise ProviderError("ChatGPT returned no text (status: %s)." % detail)
    return text


def call_gemini(key, model, system, messages):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["text"]}]}
            for m in messages
        ],
    }
    url = "%s/v1beta/models/%s:generateContent" % (
        PROVIDERS["gemini"]["base"], urllib.parse.quote(model, safe=""))
    data = http_json(url, {"x-goog-api-key": key}, body)
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
        raise ProviderError("Gemini returned no reply (%s)." % reason)
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise ProviderError("Gemini returned no text (finish reason: %s)." % cands[0].get("finishReason"))
    if cands[0].get("finishReason") == "MAX_TOKENS":
        text += "\n\n*[Reply cut off at the output limit.]*"
    return text


CALLERS = {"claude": call_claude, "chatgpt": call_chatgpt, "gemini": call_gemini}


def list_models(provider, key):
    base = PROVIDERS[provider]["base"]
    if provider == "claude":
        data = http_json(base + "/v1/models?limit=1000", claude_headers(key))
        ids = [m.get("id", "") for m in data.get("data", [])]
    elif provider == "chatgpt":
        data = http_json(base + "/v1/models", {"Authorization": "Bearer " + key})
        skip = ("image", "audio", "realtime", "tts", "transcribe", "embedding", "moderation",
                "whisper", "dall-e", "sora", "live", "search")
        ids = [m.get("id", "") for m in data.get("data", [])]
        ids = [i for i in ids if i.startswith(("gpt-", "o1", "o3", "o4", "chat")) and not any(s in i for s in skip)]
    else:
        data = http_json(base + "/v1beta/models?pageSize=1000", {"x-goog-api-key": key})
        ids = [m.get("name", "").replace("models/", "", 1) for m in data.get("models", [])
               if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        ids = [i for i in ids if i.startswith("gemini")]
    return sorted({i for i in ids if i}, reverse=True)


# ------------------------------------------------------------ conversations ---

ID_RE = re.compile(r"^[a-z0-9]{6,32}$")


def conv_path(cid):
    if not ID_RE.match(cid or ""):
        raise ValueError("bad conversation id")
    return CONV_DIR / (cid + ".json")


def list_conversations():
    out = []
    if CONV_DIR.is_dir():
        for p in CONV_DIR.glob("*.json"):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append({"id": c["id"], "title": c.get("title") or "Untitled",
                            "updated": c.get("updated", 0)})
            except (OSError, ValueError, KeyError):
                continue
    out.sort(key=lambda c: c["updated"], reverse=True)
    return out


# ------------------------------------------------------------------ server ---

class Handler(BaseHTTPRequestHandler):
    server_version = "Roundtable/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers
    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _local_only(self, writing):
        """Refuse anything that isn't this machine's own browser talking to this app."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            oh = (urllib.parse.urlparse(origin).hostname or "").lower()
            if oh not in ("127.0.0.1", "localhost"):
                return False
        if writing and self.headers.get("X-Roundtable") != "1":
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 30 * 1024 * 1024:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    # -- routes
    def do_GET(self):
        if not self._local_only(False):
            return self._send(403, {"error": "forbidden"})
        url = urllib.parse.urlparse(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.encode("utf-8"), "text/html")
            if path == "/api/config":
                return self._send(200, public_config(load_config()))
            if path == "/api/models":
                provider = urllib.parse.parse_qs(url.query).get("provider", [""])[0]
                if provider not in PROVIDERS:
                    return self._send(400, {"error": "unknown provider"})
                key, _ = get_key(load_config(), provider)
                if not key:
                    return self._send(400, {"error": "Save an API key for %s first." % PROVIDERS[provider]["name"]})
                return self._send(200, {"models": list_models(provider, key)})
            if path == "/api/conversations":
                return self._send(200, {"conversations": list_conversations()})
            if path.startswith("/api/conversations/"):
                p = conv_path(path.rsplit("/", 1)[1])
                if not p.is_file():
                    return self._send(404, {"error": "not found"})
                return self._send(200, p.read_bytes())
            return self._send(404, {"error": "not found"})
        except ProviderError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:  # keep the server alive whatever happens
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        if not self._local_only(True):
            self.close_connection = True
            return self._send(403, {"error": "forbidden"})
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/chat":
                return self._chat(body)
            if path == "/api/config":
                cfg = load_config()
                if isinstance(body.get("name"), str) and body["name"].strip():
                    cfg["name"] = body["name"].strip()[:60]
                if isinstance(body.get("workspace"), str):
                    cfg["workspace"] = body["workspace"].strip()[:120]
                for pid, model in (body.get("models") or {}).items():
                    if pid in PROVIDERS and isinstance(model, str):
                        cfg["models"][pid] = model.strip()[:120]
                for pid, key in (body.get("keys") or {}).items():
                    if pid not in PROVIDERS:
                        continue
                    if key is None:
                        cfg["keys"].pop(pid, None)
                    elif isinstance(key, str) and key.strip():
                        cfg["keys"][pid] = key.strip()
                save_config(cfg)
                return self._send(200, public_config(cfg))
            if path.startswith("/api/conversations/"):
                cid = path.rsplit("/", 1)[1]
                p = conv_path(cid)
                body["id"] = cid
                body["updated"] = int(time.time() * 1000)
                CONV_DIR.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp%d" % threading.get_ident())
                tmp.write_text(json.dumps(body), encoding="utf-8")
                os.replace(tmp, p)
                return self._send(200, {"ok": True, "updated": body["updated"]})
            return self._send(404, {"error": "not found"})
        except ProviderError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_DELETE(self):
        if not self._local_only(True):
            self.close_connection = True
            return self._send(403, {"error": "forbidden"})
        path = urllib.parse.urlparse(self.path).path
        try:
            if path.startswith("/api/conversations/"):
                p = conv_path(path.rsplit("/", 1)[1])
                if p.is_file():
                    p.unlink()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def _chat(self, body):
        provider = body.get("provider")
        if provider not in PROVIDERS:
            return self._send(400, {"error": "unknown provider"})
        cfg = load_config()
        key, _ = get_key(cfg, provider)
        if not key:
            return self._send(400, {"error": "No API key saved for %s. Open Settings to add one."
                                    % PROVIDERS[provider]["name"]})
        messages = [m for m in (body.get("messages") or [])
                    if m.get("role") in ("user", "assistant") and (m.get("text") or "").strip()]
        if not messages or messages[-1]["role"] != "user":
            return self._send(400, {"error": "nothing to respond to"})
        model = get_model(cfg, provider)
        text = CALLERS[provider](key, model, body.get("system") or "", messages)
        return self._send(200, {"text": text, "model": model})


def main():
    ap = argparse.ArgumentParser(description="Roundtable: one conversation with Claude, ChatGPT and Gemini.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit("Couldn't start on port %d (%s). Try: python roundtable.py --port 8766" % (args.port, e))
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d" % args.port
    print("Roundtable is running at %s" % url)
    print("Data folder: %s" % DATA_DIR)
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# -------------------------------------------------------------------- page ---

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roundtable</title>
<style>
:root{
  --bg:#f6f5f1; --panel:#ffffff; --ink:#1d1d1b; --muted:#6b6a65; --line:#e2e0d8; --soft:#efede6;
  --claude:#b8562f; --chatgpt:#1f7a5c; --gemini:#3a5fc4; --you:#1d1d1b; --err:#a62b2b;
  --radius:10px;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#17181a; --panel:#202225; --ink:#ecebe6; --muted:#9a9993; --line:#33363a; --soft:#2a2d31;
         --claude:#e58a61; --chatgpt:#55c39c; --gemini:#86a4f5; --you:#ecebe6; --err:#f08080; }
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:flex}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:6px 12px}
button:hover:not(:disabled){border-color:var(--muted)}
button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--ink);color:var(--bg);border-color:var(--ink);font-weight:600}
button.link{border:0;background:none;padding:0;color:var(--muted);font-size:12.5px;text-decoration:underline}

#side{width:250px;flex:none;border-right:1px solid var(--line);display:flex;flex-direction:column;padding:14px 10px}
#side h1{font-size:17px;margin:0 6px 12px;letter-spacing:.01em}
#side h1 small{display:block;font-weight:400;color:var(--muted);font-size:12px}
#convs{flex:1;overflow:auto;margin-top:10px}
.conv{display:flex;align-items:center;gap:4px;padding:6px 8px;border-radius:7px;cursor:pointer;font-size:13.5px}
.conv:hover{background:var(--soft)} .conv.on{background:var(--soft);font-weight:600}
.conv span{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv button{border:0;background:none;padding:0 4px;color:var(--muted);visibility:hidden}
.conv:hover button{visibility:visible}

#main{flex:1;min-width:0;display:flex;flex-direction:column}
#top{display:flex;align-items:center;gap:8px;padding:10px 20px;border-bottom:1px solid var(--line)}
#title{flex:1;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#log{flex:1;overflow:auto;padding:20px}
#inner{max-width:1500px;margin:0 auto}
.empty{color:var(--muted);max-width:560px;margin:8vh auto;text-align:center}
.empty h2{color:var(--ink);font-size:20px;margin-bottom:6px}

.human{margin:22px 0 10px;padding:12px 16px;background:var(--soft);border-radius:var(--radius);border-left:3px solid var(--you)}
.who{font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;margin-bottom:2px}
.who small{font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted);margin-left:6px}
.human .txt{white-space:pre-wrap;overflow-wrap:anywhere}
.directive{margin:18px 0 10px;display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12.5px;text-transform:uppercase;letter-spacing:.06em}
.directive:before,.directive:after{content:"";flex:1;border-top:1px solid var(--line)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(var(--n),minmax(0,1fr));align-items:start}
@media (max-width:1000px){ .grid{grid-template-columns:1fr} #side{display:none} }
.card{background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--c);border-radius:var(--radius);padding:12px 14px;min-width:0}
.card .who{color:var(--c);display:flex;align-items:baseline}
.card .who .sp{flex:1}
.card.claude{--c:var(--claude)} .card.chatgpt{--c:var(--chatgpt)} .card.gemini{--c:var(--gemini)}
.card .err{color:var(--err);font-size:13.5px;overflow-wrap:anywhere}
.dots{color:var(--muted)} .dots i{animation:b 1.2s infinite;font-style:normal;opacity:.2}
.dots i:nth-child(2){animation-delay:.2s} .dots i:nth-child(3){animation-delay:.4s}
@keyframes b{50%{opacity:1}}

.md{overflow-wrap:anywhere}
.md>:first-child{margin-top:4px} .md>:last-child{margin-bottom:0}
.md p{margin:.55em 0} .md ul,.md ol{margin:.5em 0;padding-left:1.4em} .md li{margin:.2em 0} .md li p{margin:.2em 0}
.md h3,.md h4,.md h5,.md h6{margin:.9em 0 .3em;font-size:1em} .md h3{font-size:1.1em}
.md code{font:12.5px/1.4 ui-monospace,Consolas,Menlo,monospace;background:var(--soft);padding:1px 4px;border-radius:4px}
.md pre{background:var(--soft);padding:10px 12px;border-radius:8px;overflow:auto} .md pre code{background:none;padding:0}
.md blockquote{margin:.6em 0;padding-left:12px;border-left:3px solid var(--line);color:var(--muted)}
.md table{border-collapse:collapse;display:block;overflow:auto;font-size:13.5px;margin:.6em 0}
.md th,.md td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
.md hr{border:0;border-top:1px solid var(--line);margin:1em 0}
.md a{color:inherit}

#composer{border-top:1px solid var(--line);padding:12px 20px 16px;background:var(--bg)}
#cin{max-width:1500px;margin:0 auto}
#msg{width:100%;min-height:64px;max-height:40vh;resize:vertical;padding:10px 12px;border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);outline:none}
#msg:focus{border-color:var(--muted)}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px}
.row .sp{flex:1}
.chk{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border:1px solid var(--line);border-radius:99px;background:var(--panel);font-size:13.5px;cursor:pointer;user-select:none}
.chk input{accent-color:var(--c);margin:0}
.chk.claude{--c:var(--claude)} .chk.chatgpt{--c:var(--chatgpt)} .chk.gemini{--c:var(--gemini)}
.chk b{color:var(--c)}
.hint{color:var(--muted);font-size:12.5px}
select{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:6px 8px}
#notice{color:var(--err);font-size:13px;min-height:1.2em;margin-top:4px}

#modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;padding:20px;z-index:5}
#modal.on{display:flex}
#sheet{background:var(--panel);border-radius:14px;padding:22px 24px;width:min(640px,100%);max-height:92vh;overflow:auto;border:1px solid var(--line)}
#sheet h2{margin:0 0 4px;font-size:18px}
#sheet p{color:var(--muted);font-size:13.5px;margin:4px 0 14px}
fieldset{border:1px solid var(--line);border-radius:10px;margin:0 0 12px;padding:10px 14px 14px}
legend{font-weight:700;padding:0 6px}
label{display:block;font-size:12.5px;color:var(--muted);margin:8px 0 3px}
#sheet input[type=text],#sheet input[type=password]{width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.inl{display:flex;gap:8px;align-items:center} .inl input{flex:1}
.stat{font-size:12.5px;color:var(--muted);margin-left:8px;font-weight:400}
</style>
</head>
<body>
<aside id="side">
  <h1>Roundtable<small>Claude · ChatGPT · Gemini · you</small></h1>
  <button id="new" class="primary">New conversation</button>
  <div id="convs"></div>
  <button id="openSettings">Settings</button>
</aside>
<section id="main">
  <div id="top">
    <div id="title">New conversation</div>
    <button id="export">Export</button>
    <button id="openSettings2">Settings</button>
  </div>
  <div id="log"><div id="inner"></div></div>
  <div id="composer"><div id="cin">
    <textarea id="msg" placeholder="Ask the table something. Enter sends, Shift+Enter adds a line."></textarea>
    <div class="row">
      <span class="hint">Who answers:</span>
      <span id="checks"></span>
      <span class="sp"></span>
      <button id="cross" title="Each model reads the others' latest answers and critiques them">Cross-examine</button>
      <select id="synthWho" title="Who writes the synthesis"></select>
      <button id="synth" title="One model sums up agreement, disputes and a best answer">Synthesize</button>
      <button id="send" class="primary">Send</button>
    </div>
    <div id="notice"></div>
  </div></div>
</section>

<div id="modal"><div id="sheet">
  <h2>Settings</h2>
  <p>Keys are saved in a file on this computer and sent only to each provider's own API. They are never shown again after saving.</p>
  <label for="sName">Your name, as the models will see it</label>
  <input type="text" id="sName" maxlength="60">
  <div id="provs" style="margin-top:14px"></div>
  <div class="row"><span id="sMsg" class="hint"></span><span class="sp"></span>
    <button id="sCancel">Close</button><button id="sSave" class="primary">Save</button></div>
</div></div>

<script>
"use strict";
const ORDER = ['claude','chatgpt','gemini'];
const NAMES = {claude:'Claude', chatgpt:'ChatGPT', gemini:'Gemini'};
const KEY_HELP = {claude:'console.anthropic.com', chatgpt:'platform.openai.com', gemini:'aistudio.google.com'};
const CROSS_TEXT = "Cross-examination round. Read the other participants' most recent answers above. " +
  "Point out specific errors, gaps or unsupported claims; say where you agree and where you disagree; " +
  "then state whether you are keeping or changing your position, and why. Do not agree just to be agreeable. " +
  "If you still think you are right, defend it.";
const synthText = n => "Write a synthesis of the discussion so far for " + n + ": (1) what the participants agree on, " +
  "(2) what is still in dispute, with the strongest case for each side, (3) your best overall answer, and " +
  "(4) what evidence or test would settle the open questions. Be fair to positions you argued against.";

let cfg = null, conv = null, busy = 0;
const $ = id => document.getElementById(id);

/* ---------- local server ---------- */
async function api(path, opts){
  opts = opts || {};
  let r;
  try{
    r = await fetch(path, {method: opts.method || 'GET',
      headers: {'Content-Type':'application/json','X-Roundtable':'1'},
      body: opts.body ? JSON.stringify(opts.body) : undefined});
  }catch(e){ throw new Error("Can't reach the Roundtable program. Is roundtable.py still running?"); }
  let data;
  try{ data = await r.json(); }catch(e){ data = {error:'Unreadable reply from the local server'}; }
  if(!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}

/* ---------- prompts ---------- */
function systemPrompt(self){
  const others = ORDER.filter(p => p !== self).map(p => NAMES[p]);
  return "You are " + NAMES[self] + ", taking part in a roundtable discussion with a human moderator (" + cfg.name +
    ") and two other AI models, " + others[0] + " and " + others[1] + ".\n\n" +
    "How the transcript works: your own earlier replies appear as your turns. Everything else arrives labeled in " +
    "square brackets, for example \"[" + cfg.name + "]:\" for the moderator and \"[" + others[0] + "]:\" for another model.\n\n" +
    "Ground rules:\n" +
    "- Speak only for yourself. Do not start your reply with your own name, and never write lines for anyone else.\n" +
    "- First answers to a question are written independently; you see the others' answers afterward.\n" +
    "- The goal is the correct answer, not agreement. If another participant is wrong, say so and say exactly why. " +
    "Do not soften a position because you are outnumbered.\n" +
    "- If someone's argument changes your mind, say what specifically changed it.\n" +
    "- State uncertainty plainly. Be concise, and do not recap what others said unless you are responding to it.";
}

function nameList(ids){ return ids.map(p => NAMES[p]).join(' and '); }

function buildMessages(events, self){
  const msgs = []; let buf = [];
  const flush = () => { if(buf.length){ msgs.push({role:'user', text: buf.join('\n\n')}); buf = []; } };
  for(const e of events){
    if(e.type === 'human'){
      const to = (e.to && e.to.length < ORDER.length) ? ', addressing ' + nameList(e.to) : '';
      buf.push('[' + cfg.name + to + ']: ' + e.text);
    }else if(e.type === 'directive'){
      const to = !e.only ? 'all participants' : (e.only === self ? 'you' : NAMES[e.only]);
      buf.push('[Moderator instruction to ' + to + ']: ' + e.text);
    }else if(e.type === 'ai' && e.text && !e.error){
      if(e.who === self){
        flush();
        if(!msgs.length) msgs.push({role:'user', text:'(start of discussion)'});
        const last = msgs[msgs.length - 1];
        if(last.role === 'assistant') last.text += '\n\n' + e.text;
        else msgs.push({role:'assistant', text: e.text});
      }else buf.push('[' + NAMES[e.who] + ']: ' + e.text);
    }
  }
  flush();
  return msgs;
}

/* ---------- running a round ---------- */
// Each model gets the transcript only up to the prompt that opened its round,
// so answers within a round are written without seeing each other.
function roundStart(ev){
  const idx = conv.events.indexOf(ev);
  for(let j = idx - 1; j >= 0; j--){
    const t = conv.events[j].type;
    if(t === 'human' || t === 'directive') return j;
  }
  return -1;
}

async function ask(ev){
  const c = conv;
  const snapshot = c.events.slice(0, roundStart(ev) + 1);
  ev.pending = true; ev.error = null; ev.text = '';
  setBusy(1); render();
  try{
    const r = await api('/api/chat', {method:'POST', body:{provider: ev.who, system: systemPrompt(ev.who),
                                                           messages: buildMessages(snapshot, ev.who)}});
    ev.text = r.text; ev.model = r.model;
  }catch(e){ ev.error = e.message; }
  ev.pending = false;
  setBusy(-1);
  if(conv === c) render();
  save(c);
}

function runRound(targets){
  const evs = targets.map(who => ({type:'ai', who: who, model: cfg.providers[who].model, text:'', pending:true}));
  evs.forEach(e => conv.events.push(e));
  scrollDown(true);
  return Promise.all(evs.map(ask));
}

function checked(){ return ORDER.filter(p => $('chk_' + p).checked); }
function note(s){ $('notice').textContent = s || ''; }

function send(){
  if(busy) return;
  const text = $('msg').value.trim(), targets = checked();
  if(!text) return note('Type a message first.');
  if(!targets.length) return note('Tick at least one model to answer.');
  note('');
  const ev = {type:'human', text: text};
  if(targets.length < ORDER.length) ev.to = targets;
  conv.events.push(ev);
  if(!conv.title){ conv.title = text.replace(/\s+/g,' ').slice(0, 70); }
  $('msg').value = '';
  runRound(targets).then(refreshList);
}

function hasReplies(){ return conv.events.some(e => e.type === 'ai' && e.text && !e.error); }

function cross(){
  if(busy) return;
  const targets = checked();
  if(!hasReplies()) return note('Ask a question first, then cross-examine the answers.');
  if(!targets.length) return note('Tick at least one model.');
  note('');
  conv.events.push({type:'directive', kind:'cross', text: CROSS_TEXT});
  runRound(targets);
}

function synth(){
  if(busy) return;
  if(!hasReplies()) return note('Nothing to synthesize yet.');
  note('');
  const who = $('synthWho').value;
  conv.events.push({type:'directive', kind:'synth', only: who, text: synthText(cfg.name)});
  runRound([who]);
}

function setBusy(d){
  busy += d;
  ['send','cross','synth'].forEach(id => $(id).disabled = busy > 0);
}

/* ---------- markdown (small, safe subset) ---------- */
function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function inline(s){
  s = esc(s);
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (m, c) => { codes.push(c); return '\u0001' + (codes.length - 1) + '\u0001'; });
  s = s.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?![*\w])/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s.replace(/\u0001(\d+)\u0001/g, (m, i) => '<code>' + codes[+i] + '</code>');
}
const RE_LI = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const RE_HR = /^\s*([-*_])(\s*\1){2,}\s*$/;
function isBlockStart(l){
  return /^\s*```/.test(l) || /^#{1,6}\s/.test(l) || /^\s*>/.test(l) || (RE_LI.test(l) && !RE_HR.test(l)) || RE_HR.test(l);
}
function md(src){
  const L = src.replace(/\r\n?/g, '\n').split('\n'), out = [];
  let i = 0, m;
  while(i < L.length){
    const l = L[i];
    if(/^\s*```/.test(l)){
      const b = []; i++;
      while(i < L.length && !/^\s*```/.test(L[i])) b.push(L[i++]);
      i++; out.push('<pre><code>' + esc(b.join('\n')) + '</code></pre>'); continue;
    }
    if(!l.trim()){ i++; continue; }
    if((m = l.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/))){
      const n = Math.min(m[1].length + 2, 6);
      out.push('<h' + n + '>' + inline(m[2]) + '</h' + n + '>'); i++; continue;
    }
    if(RE_HR.test(l)){ out.push('<hr>'); i++; continue; }
    if(/^\s*\|.*\|\s*$/.test(l) && i + 1 < L.length && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(L[i + 1])){
      const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => inline(c.trim()));
      let h = '<table><thead><tr>' + cells(l).map(c => '<th>' + c + '</th>').join('') + '</tr></thead><tbody>';
      i += 2;
      while(i < L.length && /^\s*\|.*\|\s*$/.test(L[i])){
        h += '<tr>' + cells(L[i]).map(c => '<td>' + c + '</td>').join('') + '</tr>'; i++;
      }
      out.push(h + '</tbody></table>'); continue;
    }
    if(/^\s*>/.test(l)){
      const b = [];
      while(i < L.length && /^\s*>/.test(L[i])) b.push(L[i++].replace(/^\s*>\s?/, ''));
      out.push('<blockquote>' + md(b.join('\n')) + '</blockquote>'); continue;
    }
    if((m = l.match(RE_LI))){
      const base = m[1].length, ordered = /\d/.test(m[2]), items = [];
      while(i < L.length){
        const k = L[i].match(RE_LI);
        if(k && k[1].length <= base + 1 && !RE_HR.test(L[i])){
          if(/\d/.test(k[2]) !== ordered) break;
          items.push([k[3]]); i++;
        }else if(L[i].trim() && /^\s/.test(L[i]) && (L[i].match(/^\s*/)[0].length > base)){
          items[items.length - 1].push(L[i].slice(Math.min(base + 2, L[i].match(/^\s*/)[0].length))); i++;
        }else if(!L[i].trim() && i + 1 < L.length && L[i + 1].trim() &&
                 (RE_LI.test(L[i + 1]) || L[i + 1].match(/^\s*/)[0].length > base) && !RE_HR.test(L[i + 1])){
          const k2 = L[i + 1].match(RE_LI);
          if(k2 && k2[1].length <= base + 1 && /\d/.test(k2[2]) !== ordered) break;
          if(!(k2 && k2[1].length <= base + 1)) items[items.length - 1].push('');
          i++;
        }else break;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push('<' + tag + '>' + items.map(it => '<li>' + (it.length === 1 ? inline(it[0]) : md(it.join('\n'))) + '</li>').join('') + '</' + tag + '>');
      continue;
    }
    const p = [l]; i++;
    while(i < L.length && L[i].trim() && !isBlockStart(L[i])) p.push(L[i++]);
    out.push('<p>' + p.map(inline).join('<br>') + '</p>');
  }
  return out.join('');
}

/* ---------- rendering ---------- */
function el(tag, cls, text){
  const e = document.createElement(tag);
  if(cls) e.className = cls;
  if(text != null) e.textContent = text;
  return e;
}
function scrollDown(force){
  const log = $('log');
  if(force || log.scrollHeight - log.scrollTop - log.clientHeight < 160)
    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
}
function card(ev){
  const c = el('div', 'card ' + ev.who);
  const w = el('div', 'who'); w.append(NAMES[ev.who]);
  w.append(el('small', '', ev.model || '')); w.append(el('span', 'sp'));
  if(ev.text && !ev.pending){
    const b = el('button', 'link', 'copy');
    b.onclick = () => { navigator.clipboard.writeText(ev.text).then(() => { b.textContent = 'copied'; setTimeout(() => b.textContent = 'copy', 1200); }); };
    w.append(b);
  }
  c.append(w);
  if(ev.pending){ const d = el('div', 'dots'); d.innerHTML = 'thinking<i>.</i><i>.</i><i>.</i>'; c.append(d); }
  else if(ev.error){
    c.append(el('div', 'err', ev.error));
    const r = el('button', '', 'Retry'); r.style.marginTop = '8px'; r.onclick = () => ask(ev); c.append(r);
  }else{ const d = el('div', 'md'); d.innerHTML = md(ev.text); c.append(d); }
  return c;
}
function render(){
  const inner = $('inner'), log = $('log');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 160;
  const keep = log.scrollTop;
  inner.textContent = '';
  $('title').textContent = conv.title || 'New conversation';
  document.title = (conv.title ? conv.title + ' - ' : '') + 'Roundtable';
  if(!conv.events.length){
    const e = el('div', 'empty');
    e.innerHTML = '<h2>Put a question to the table</h2>' +
      '<p>All three models answer on their own first, without seeing each other. ' +
      'Then use <b>Cross-examine</b> to have them critique one another, keep questioning them yourself, ' +
      'and ask one for a <b>synthesis</b> when the argument has run its course.</p>';
    inner.append(e); return;
  }
  let grid = null;
  for(const ev of conv.events){
    if(ev.type === 'ai'){
      if(!grid){ grid = el('div', 'grid'); grid._n = 0; inner.append(grid); }
      grid.append(card(ev)); grid._n++; grid.style.setProperty('--n', Math.min(grid._n, 3));
      continue;
    }
    grid = null;
    if(ev.type === 'human'){
      const h = el('div', 'human'), w = el('div', 'who', cfg.name);
      if(ev.to && ev.to.length < ORDER.length) w.append(el('small', '', 'to ' + nameList(ev.to)));
      h.append(w); h.append(el('div', 'txt', ev.text)); inner.append(h);
    }else{
      inner.append(el('div', 'directive', ev.kind === 'synth' ? 'Synthesis by ' + NAMES[ev.only] : 'Cross-examination'));
    }
  }
  if(atBottom) log.scrollTop = log.scrollHeight; else log.scrollTop = keep;
}

/* ---------- conversations ---------- */
function newConv(){
  conv = {id: Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4),
          title: '', created: Date.now(), events: []};
  render(); refreshList(); $('msg').focus();
}
async function save(c){
  if(!c.events.length) return;
  const clean = Object.assign({}, c, {events: c.events.map(e => {
    const x = Object.assign({}, e); delete x.pending; return x; })});
  try{ await api('/api/conversations/' + c.id, {method:'POST', body: clean}); }
  catch(e){ note('Could not save the conversation: ' + e.message); }
}
async function openConv(id){
  if(busy) return note('Wait for the current replies to finish first.');
  try{
    const c = await api('/api/conversations/' + id);
    c.events = (c.events || []).filter(e => e && e.type);
    c.events.forEach(e => { if(e.type === 'ai' && !e.text && !e.error) e.error = 'No reply was received (interrupted).'; });
    conv = c; note(''); render(); refreshList(); scrollDown(true);
  }catch(e){ note(e.message); }
}
async function refreshList(){
  let list = [];
  try{ list = (await api('/api/conversations')).conversations; }catch(e){}
  const box = $('convs'); box.textContent = '';
  for(const c of list){
    const row = el('div', 'conv' + (conv && c.id === conv.id ? ' on' : ''));
    row.append(el('span', '', c.title));
    const x = el('button', '', '×'); x.title = 'Delete';
    x.onclick = async ev => {
      ev.stopPropagation();
      if(!confirm('Delete "' + c.title + '"?')) return;
      await api('/api/conversations/' + c.id, {method:'DELETE'});
      if(conv && conv.id === c.id) newConv(); else refreshList();
    };
    row.append(x); row.onclick = () => openConv(c.id); box.append(row);
  }
}
function exportMd(){
  if(!conv.events.length) return note('Nothing to export yet.');
  let out = '# ' + (conv.title || 'Roundtable') + '\n\n';
  for(const e of conv.events){
    if(e.type === 'human') out += '## ' + cfg.name + (e.to ? ' (to ' + nameList(e.to) + ')' : '') + '\n\n' + e.text + '\n\n';
    else if(e.type === 'directive') out += '---\n\n*' + (e.kind === 'synth' ? 'Synthesis by ' + NAMES[e.only] : 'Cross-examination round') + '*\n\n';
    else if(e.text) out += '### ' + NAMES[e.who] + (e.model ? ' (' + e.model + ')' : '') + '\n\n' + e.text + '\n\n';
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([out], {type:'text/markdown'}));
  a.download = (conv.title || 'roundtable').replace(/[^\w\- ]+/g, '').trim().slice(0, 50).replace(/\s+/g, '-') + '.md';
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ---------- settings ---------- */
function keyStatus(p){
  const s = cfg.providers[p];
  return s.keySource === 'file' ? 'key saved' : s.keySource === 'env' ? 'using environment variable' : 'no key yet';
}
function buildSettings(){
  $('sName').value = cfg.name; $('sMsg').textContent = '';
  const box = $('provs'); box.textContent = '';
  for(const p of ORDER){
    const f = el('fieldset');
    const lg = el('legend', '', NAMES[p]); lg.append(el('span', 'stat', keyStatus(p))); f.append(lg);
    f.append(el('label', '', 'API key (from ' + KEY_HELP[p] + ')'));
    const row = el('div', 'inl');
    const k = el('input'); k.type = 'password'; k.id = 'key_' + p; k.autocomplete = 'off';
    k.placeholder = cfg.providers[p].hasKey ? 'Leave blank to keep the current key' : 'Paste key';
    row.append(k);
    if(cfg.providers[p].keySource === 'file'){
      const rm = el('button', '', 'Remove');
      rm.onclick = async () => { const keys = {}; keys[p] = null; cfg = await api('/api/config', {method:'POST', body:{keys: keys}}); buildSettings(); };
      row.append(rm);
    }
    f.append(row);
    f.append(el('label', '', 'Model'));
    const row2 = el('div', 'inl');
    const m = el('input'); m.type = 'text'; m.id = 'model_' + p; m.value = cfg.providers[p].model; m.setAttribute('list', 'dl_' + p);
    const dl = el('datalist'); dl.id = 'dl_' + p;
    const lb = el('button', '', 'List models');
    lb.onclick = async () => {
      lb.disabled = true; $('sMsg').textContent = 'Fetching ' + NAMES[p] + ' models...';
      try{
        const r = await api('/api/models?provider=' + p);
        dl.textContent = ''; r.models.forEach(id => { const o = el('option'); o.value = id; dl.append(o); });
        $('sMsg').textContent = r.models.length + ' models found. Clear the Model box and click it to pick one.';
      }catch(e){ $('sMsg').textContent = e.message; }
      lb.disabled = false;
    };
    row2.append(m, dl, lb); f.append(row2);
    if(p === 'claude'){
      f.append(el('label', '', 'Workspace ID (only needed if the key is not tied to a workspace)'));
      const w = el('input'); w.type = 'text'; w.id = 'sWorkspace'; w.value = cfg.workspace || ''; w.placeholder = 'wrkspc_...';
      f.append(w);
    }
    box.append(f);
  }
}
async function saveSettings(){
  const body = {name: $('sName').value, workspace: $('sWorkspace').value, models: {}, keys: {}};
  for(const p of ORDER){
    body.models[p] = $('model_' + p).value;
    if($('key_' + p).value.trim()) body.keys[p] = $('key_' + p).value.trim();
  }
  try{
    cfg = await api('/api/config', {method:'POST', body: body});
    buildSettings(); $('sMsg').textContent = 'Saved.'; render();
  }catch(e){ $('sMsg').textContent = e.message; }
}
function showSettings(on){ if(on) buildSettings(); $('modal').classList.toggle('on', on); }

/* ---------- start ---------- */
async function init(){
  cfg = await api('/api/config');
  for(const p of ORDER){
    const l = el('label', 'chk ' + p); const c = el('input'); c.type = 'checkbox'; c.id = 'chk_' + p; c.checked = true;
    l.append(c, el('b', '', NAMES[p])); $('checks').append(l, ' ');
    const o = el('option', '', NAMES[p]); o.value = p; $('synthWho').append(o);
  }
  $('send').onclick = send; $('cross').onclick = cross; $('synth').onclick = synth;
  $('new').onclick = () => { if(busy) return note('Wait for the current replies to finish first.'); newConv(); };
  $('export').onclick = exportMd;
  $('openSettings').onclick = $('openSettings2').onclick = () => showSettings(true);
  $('sCancel').onclick = () => showSettings(false); $('sSave').onclick = saveSettings;
  $('modal').onclick = e => { if(e.target === $('modal')) showSettings(false); };
  $('msg').addEventListener('keydown', e => {
    if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); send(); }
  });
  newConv();
  if(!ORDER.some(p => cfg.providers[p].hasKey)) showSettings(true);
}
init().catch(e => { document.body.textContent = 'Roundtable failed to start: ' + e.message; });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
