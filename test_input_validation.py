#!/usr/bin/env python3
"""
Input-validation / robustness regression tests for EMBR.

Reproduces the malformed/extreme inputs from the 2026-07 bug review and asserts
each is now rejected or handled cleanly (no hang, no crash, no stack-trace leak),
while valid requests still succeed with unchanged results.

Run:  python test_input_validation.py    (exit 0 = all pass)
"""
import importlib.util as u, json, socket, threading, time, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = u.spec_from_file_location("embr_server", os.path.join(HERE, "embr-server.py"))
embr = u.module_from_spec(_spec); _spec.loader.exec_module(embr)

HOST, PORT = "127.0.0.1", 8199
_httpd = ThreadingHTTPServer((HOST, PORT), embr.AmpacityHandler)
threading.Thread(target=_httpd.serve_forever, daemon=True).start()
time.sleep(0.3)

MV = dict(systemType="mv", installType="conduit", cableSize="500", burialDepth=36,
          soilTemp=25, soilRhoNative=0.9, soilRhoDry=2.5, useDryout=True, loadFactor=1.0,
          numCircuits=2, circuitSpacing=12, conduitType="pvc40", conduitSize=3)
DC = dict(systemType="dc", installType="direct", cableSize="500", material="cu",
          insulation="xhhw", conductorsPerConduit=1, burialDepth=36, soilTemp=25,
          soilRhoNative=0.9, soilRhoDry=2.5, useDryout=False, loadFactor=1.0, numCircuits=1)
LV = dict(systemType="lvac", installType="conduit", cableSize="350", material="al",
          insulation="thwn", phaseConfig="three_phase_wye", neutralFactor=0.5, burialDepth=30,
          soilTemp=20, soilRhoNative=0.9, soilRhoDry=2.0, useDryout=False, loadFactor=0.75,
          numCircuits=1, conduitType="hdpe", conduitSize=4)


def post(pathseg, obj, timeout=5, allow_nan=False):
    """POST JSON via urllib; returns (status, body_dict_or_text)."""
    body = json.dumps(obj, allow_nan=allow_nan).encode()
    req = urllib.request.Request("http://%s:%d/api/%s" % (HOST, PORT, pathseg),
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def raw_post(headers_block, body, timeout=5):
    """Send a hand-crafted request (to inject bogus Content-Length). Returns
    (status_int_or_None, raw_bytes). Times out -> status None (treated as hang)."""
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    s.settimeout(timeout)
    msg = (headers_block + "\r\n\r\n").encode() + body
    s.sendall(msg)
    data = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        return None, data
    finally:
        s.close()
    status = None
    if data.startswith(b"HTTP/"):
        try:
            status = int(data.split(b" ", 2)[1])
        except Exception:
            pass
    return status, data


def js(body):
    try:
        return json.loads(body)
    except Exception:
        return {}


RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


# ---------------- happy path ----------------
for label, p in [("mv", MV), ("dc", DC), ("lvac", LV)]:
    st, b = post("calculate", p)
    d = js(b)
    check("valid %s request returns 200 with an ampacity" % label,
          st == 200 and isinstance(d.get("ampacity"), (int, float)), "status=%s" % st)

# ---------------- results unchanged (no physics regression) ----------------
st, b = post("calculate", MV)
http_res = js(b)
direct_res = embr.compute_mv(MV)
check("valid result is unchanged vs direct engine call (no regression)",
      http_res.get("ampacity") == direct_res.get("ampacity") and
      http_res.get("T4t") == direct_res.get("T4t"),
      "http=%s direct=%s" % (http_res.get("ampacity"), direct_res.get("ampacity")))

# ---------------- #1 DoS: unbounded counts rejected fast ----------------
t0 = time.time(); st, b = post("calculate", {**DC, "conductorsPerConduit": 100000000})
check("huge conductorsPerConduit rejected 400 (no DoS)", st == 400 and (time.time()-t0) < 2, "status=%s" % st)
st, b = post("calculate", {**MV, "numCircuits": 999999})
check("huge numCircuits rejected 400 (no DoS)", st == 400, "status=%s" % st)

# ---------------- #10 NaN / Infinity ----------------
st, b = post("calculate", {**MV, "numCircuits": float("nan")}, allow_nan=True)
check("NaN literal in body rejected 400", st == 400, "status=%s" % st)
st, b = post("calculate", {**MV, "loadFactor": float("inf")}, allow_nan=True)
check("Infinity literal in body rejected 400", st == 400, "status=%s" % st)

# ---------------- #7 negative conduitSize ----------------
st, b = post("calculate", {**MV, "conduitSize": -1})
check("negative conduitSize rejected 400 (no silent wrap)", st == 400, "status=%s" % st)

# ---------------- #6 bad conduitType ----------------
st, b = post("calculate", {**MV, "conduitType": "steel"})
check("invalid conduitType rejected 400 (no silent model switch)", st == 400, "status=%s" % st)

# ---------------- #8 bad phaseConfig ----------------
st, b = post("calculate", {**LV, "phaseConfig": "typo"})
check("invalid phaseConfig rejected 400 (not silent wye)", st == 400, "status=%s" % st)

# ---------------- #9 non-positive depth / rho / load factor ----------------
st, _ = post("calculate", {**MV, "burialDepth": 0})
check("zero burialDepth rejected 400", st == 400, "status=%s" % st)
st, _ = post("calculate", {**MV, "soilRhoNative": 0})
check("zero soilRhoNative rejected 400", st == 400, "status=%s" % st)
st, _ = post("calculate", {**MV, "loadFactor": 5})
check("out-of-range loadFactor rejected 400", st == 400, "status=%s" % st)

# ---------------- #5 unknown systemType ----------------
st, _ = post("calculate", {**MV, "systemType": "xyz"})
check("unknown systemType returns 400 (not 200)", st == 400, "status=%s" % st)

# bad cableSize
st, _ = post("calculate", {**MV, "cableSize": "9999"})
check("invalid MV cableSize rejected 400", st == 400, "status=%s" % st)

# ---------------- malformed JSON ----------------
st, data = raw_post("POST /api/calculate HTTP/1.0\r\nContent-Length: 5", b"{bad}")
check("malformed JSON body returns 400 (no crash)", st == 400, "status=%s" % st)

# ---------------- #2 Content-Length abuse ----------------
st, data = raw_post("POST /api/calculate HTTP/1.0\r\nContent-Length: -1", b"")
check("negative Content-Length rejected 400 (no hang)", st == 400, "status=%s (None=hang)" % st)
st, data = raw_post("POST /api/calculate HTTP/1.0\r\nContent-Length: notanumber", b"")
check("non-numeric Content-Length rejected 400", st == 400, "status=%s" % st)
st, data = raw_post("POST /api/calculate HTTP/1.0\r\nContent-Length: 300000", b"{}")
check("oversize body rejected 413", st == 413, "status=%s" % st)

# ---------------- #3 export-pdf must not leak a stack trace ----------------
st, b = post("export-pdf", {"params": MV, "result": {"systemType": "mv", "ampacity": 100,
             "cableOD": "abc", "cableLabel": "x"}, "project": {}})
body_text = b.decode("utf-8", "replace")
leaks = ("Traceback" in body_text or "embr-server.py" in body_text or
         "/sessions/" in body_text or "line " in body_text)
check("export-pdf error does NOT leak a stack trace / file paths",
      st == 500 and not leaks, "status=%s leak=%s body=%s" % (st, leaks, body_text[:80]))

# export-pdf happy path
st, b = post("calculate", MV); res = js(b)
st, b = post("export-pdf", {"params": MV, "result": res, "project": {"projectNumber": "T"}})
check("export-pdf valid request returns a PDF", st == 200 and b[:5] == b"%PDF-", "status=%s head=%r" % (st, b[:5]))

# ---------------- 15/25 kV MV cables + operating voltage ----------------
MV15 = dict(MV, cableSize="15kv_500", voltage_kv=13.8)
st, b = post("calculate", MV15); d = js(b)
check("15 kV 500 kcmil returns 200 with an ampacity",
      st == 200 and isinstance(d.get("ampacity"), (int, float)), "status=%s" % st)
check("15 kV result reports its voltage class", d.get("voltageClass") == "15kV", "vc=%s" % d.get("voltageClass"))
st, b = post("calculate", dict(MV, cableSize="25kv_1000", voltage_kv=24.0)); d = js(b)
check("25 kV 1000 kcmil returns 200 with an ampacity",
      st == 200 and isinstance(d.get("ampacity"), (int, float)), "status=%s" % st)
# operating voltage above the cable's insulation class is rejected
st, _ = post("calculate", dict(MV, cableSize="15kv_500", voltage_kv=34.5))
check("operating voltage above the 15 kV class is rejected 400", st == 400, "status=%s" % st)
# operating voltage out of the absolute range is rejected
st, _ = post("calculate", dict(MV, cableSize="500", voltage_kv=100))
check("operating voltage 100 kV rejected 400", st == 400, "status=%s" % st)
# operating voltage changes dielectric loss (Wd) on the same cable
d1 = js(post("calculate", dict(MV, cableSize="25kv_1000", voltage_kv=13.8))[1])
d2 = js(post("calculate", dict(MV, cableSize="25kv_1000", voltage_kv=24.0))[1])
check("higher operating voltage yields higher dielectric loss", d2.get("Wd", 0) > d1.get("Wd", 0),
      "Wd(13.8)=%s Wd(24)=%s" % (d1.get("Wd"), d2.get("Wd")))


# ---------------- BE-2: same-origin (no wildcard CORS) ----------------
def _headers(method, pathseg, api=True):
    url = "http://%s:%d/%s" % (HOST, PORT, ("api/" + pathseg) if api else pathseg)
    req = urllib.request.Request(url, method=method)
    if method == "POST":
        req.data = json.dumps(MV).encode(); req.add_header("Content-Type", "application/json")
    try:
        r = urllib.request.urlopen(req, timeout=5); return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers

st, h = _headers("POST", "calculate")
check("calculate response carries no wildcard CORS header (same-origin)",
      st == 200 and h.get("Access-Control-Allow-Origin") is None,
      "status=%s acao=%r" % (st, h.get("Access-Control-Allow-Origin")))
st, h = _headers("OPTIONS", "calculate")
check("OPTIONS preflight carries no wildcard CORS header",
      h.get("Access-Control-Allow-Origin") is None, "acao=%r" % h.get("Access-Control-Allow-Origin"))

# ---------------- BE-3: only intended static assets are served ----------------
def _get_status(path):
    try:
        return urllib.request.urlopen("http://%s:%d%s" % (HOST, PORT, path), timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
check("GET / serves the app (200)", _get_status("/") == 200)
check("GET /embr.html serves the app (200)", _get_status("/embr.html") == 200)
check("GET /three_phase_logo.png serves the new logo (200)",
      _get_status("/three_phase_logo.png") == 200)
check("GET /gridworks_logo.png blocks the retired logo (404)",
      _get_status("/gridworks_logo.png") == 404)
st, h = _headers("GET", "healthz", api=False)
check("GET /healthz returns 200 JSON for deployment health checks",
      st == 200 and h.get_content_type() == "application/json",
      "status=%s content-type=%r" % (st, h.get("Content-Type")))
check("GET /docs/ directory listing is blocked (404)", _get_status("/docs/") == 404)
check("GET /embr-server.py source is blocked (404)", _get_status("/embr-server.py") == 404)
check("GET /requirements.txt is blocked (404)", _get_status("/requirements.txt") == 404)
check("GET /../embr-server.py traversal is blocked (404)", _get_status("/../embr-server.py") == 404)
# Home-page carousel screenshots: PNGs under /docs/screenshots are allowed; other
# extensions and traversal out of that folder are not.
check("GET /docs/screenshots/01-mv-trefoil-direct.png is served (200)",
      _get_status("/docs/screenshots/01-mv-trefoil-direct.png") == 200)
check("GET a non-image under /docs/screenshots is blocked (404)",
      _get_status("/docs/screenshots/notes.txt") == 404)
check("GET /docs/screenshots/../embr-server.py traversal is blocked (404)",
      _get_status("/docs/screenshots/../embr-server.py") == 404)

_httpd.shutdown()

# ---------------- report ----------------
print("=" * 74)
print("The Three Phase Ampacity Calculator input-validation regression tests")
print("=" * 74)
npass = 0
for name, ok, detail in RESULTS:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "   [%s]" % detail))
    npass += ok
print("-" * 74)
print("%d/%d passed" % (npass, len(RESULTS)))
import sys
sys.exit(0 if npass == len(RESULTS) else 1)
