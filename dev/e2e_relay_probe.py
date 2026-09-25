"""E2E probe: the shipped link-relay.py as a subprocess against the live app."""
import json, threading, time, subprocess, sys, os, urllib.request, httpx
import uvicorn
from app.main import app

cfg = uvicorn.Config(app, host="127.0.0.1", port=18789, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
time.sleep(2)
ok = False
try:
    with httpx.Client() as c:
        code = c.post("http://127.0.0.1:18789/api/link/start?relay=true").json()["code"]

    proc = subprocess.Popen(
        [sys.executable, "-u", "app/static/link-relay.py",
         "--server", "http://127.0.0.1:18789", "--code", code, "--port", "18208"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "BROWSER": "true"})
    buf = "".join(proc.stdout.readline() for _ in range(3))
    state = buf.split("state=")[-1].strip()
    assert len(state) == 32, repr(buf)
    assert "127.0.0.1:18208" in buf
    print("1. relay claimed link; console URL points at the RELAY's loopback port: OK")

    delivery = json.dumps({"access_token": "e2e-relay-console-token", "state": state,
                           "console_site": "international", "console_region": "ap-southeast-1"}).encode()
    req = urllib.request.Request("http://127.0.0.1:18208/", data=delivery, method="POST")
    resp = urllib.request.urlopen(req, timeout=5)
    print("2. console-style POST -> relay:", resp.status, "|", resp.read().decode())
    rc = proc.wait(timeout=15)
    tail = proc.stdout.read()
    print("3. relay exited", rc, "|", [l for l in tail.splitlines() if "forwarded" in l.lower()])

    with httpx.Client() as c:
        s = c.get("http://127.0.0.1:18789/api/link/status").json()
        print("4. dashboard:", s)
        assert s["console_linked"] is True
        b = c.get("http://127.0.0.1:18789/api/balances?refresh=true").json()
        tp = [x for x in b["providers"] if x["id"] == "alibaba-token-plan"][0]
        note = (tp.get("note") or tp.get("error") or "")
        print("5. card ok:", tp["ok"], "| note:", note[:130])
        # the fake token reached Alibaba's REAL gateway and got rejected there:
        assert "rejected" in note.lower(), note
    ok = True
finally:
    server.should_exit = True
print("E2E-RELAY-PROBE:", "PASS" if ok else "FAIL")
