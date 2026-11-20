# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
A tiny local "manager approval" web app for the human-in-the-loop gate.

The Step Functions console can't release a waitForTaskToken, so this little app
does it with a button. It uses YOUR local AWS credentials server-side (a browser
can't call AWS safely), serves a page, and:

  GET  /            -> the page: one card per recent refund-approval execution,
                       with a four-step progress strip and an event timeline
  GET  /executions  -> read-only JSON the page polls (Step Functions history)
  POST /approve     -> look up the task token and call SendTaskSuccess

The state machine is found from the STATE_MACHINE_ARN environment variable, or
built as arn:aws:states:<region>:<your account>:stateMachine:refund-approval.

Run it — it starts the server AND opens the browser:

    python approve_ui.py
"""

import base64
import collections
import hashlib
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")
APPROVALS_TABLE = "refund-approvals"
STATE_MACHINE_NAME = "refund-approval"   # fixed by demo-setup/refund-workflow/template.yaml
APPROVAL_TIMEOUT_SECONDS = 3600          # TimeoutSeconds of the ManagerApproval state
MAX_EXECUTIONS = 15                      # most recent executions shown
MAX_HISTORY_PAGES = 5                    # hard cap on GetExecutionHistory pages per execution
MAX_INPUT_CHARS = 4096                   # ignore larger execution inputs
MAX_BODY_BYTES = 1024                    # largest accepted POST /approve body
SNAPSHOT_TTL = 2.0                       # seconds; extra tabs reuse one AWS round trip
CACHE_LIMIT = 50                         # finished executions kept in memory


def _port_from_env():
    """APPROVE_PORT (default 8000), validated as a TCP port; exits cleanly if not."""
    raw = os.environ.get("APPROVE_PORT", "8000").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        sys.exit(f"APPROVE_PORT must be a port number from 1 to 65535 (got {raw!r}).")
    return int(raw)


PORT = _port_from_env()
# Loopback only. This app acts with your AWS credentials and has no login.
BIND_HOST = "127.0.0.1"

# Same rule the refund Lambda enforces: 1-64 letters, digits, or hyphens.
ORDER_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")

# Browsers let any open web page send requests to http://localhost. These apps
# act with your AWS credentials, so only serve requests that come from this
# app's own page: the Host must be this server (blocks DNS rebinding), and the
# browser's Origin / Sec-Fetch-Site headers, when present, must be same-origin
# (blocks cross-site request forgery). Non-browser clients (curl) send neither.
LOCAL_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}


def is_local_same_origin(headers):
    """True if the request targets this local server and isn't cross-site."""
    if headers.get("Host", "") not in LOCAL_HOSTS:
        return False
    origin = headers.get("Origin")
    if origin is not None and origin not in {f"http://{h}" for h in LOCAL_HOSTS}:
        return False
    return headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")


# Adaptive retries back off when AWS throttles. Tests replace these three names.
_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 5})
ddb = boto3.client("dynamodb", region_name=REGION, config=_CONFIG)
sfn = boto3.client("stepfunctions", region_name=REGION, config=_CONFIG)
sts = boto3.client("sts", region_name=REGION, config=_CONFIG)

# ---------------------------------------------------------------------------
# Step Functions data -> the four-box view
# ---------------------------------------------------------------------------
BOXES = [("ValidateOrder", "Validate"), ("FraudCheck", "Fraud check"),
         ("ManagerApproval", "Manager approval"), ("ProcessRefund", "Process refund")]
LABELS = dict(BOXES)
FINISHED = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
STATE_ENTERED = {"PassStateEntered", "TaskStateEntered"}
STATE_EXITED = {"PassStateExited", "TaskStateExited"}
TASK_FAILURES = {"TaskFailed", "TaskStartFailed", "TaskSubmitFailed", "TaskTimedOut",
                 "TaskStateAborted", "EvaluationFailed"}
EXEC_FAILURES = {"ExecutionFailed", "ExecutionTimedOut", "ExecutionAborted"}
_LONG_TOKENISH = re.compile(r"[A-Za-z0-9+/=_-]{80,}")


class AppError(Exception):
    """An error that is safe to show on the page as one line."""


def _clean(text):
    """One-line, token-free, length-bounded text for the timeline."""
    text = _LONG_TOKENISH.sub("[redacted]", " ".join(str(text or "").split()))
    return text[:200]


def aws_error_line(e):
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "Error")
        if code == "StateMachineDoesNotExist":
            return (f"State machine {STATE_MACHINE_NAME} not found in {REGION}; "
                    "run setup.sh or set STATE_MACHINE_ARN")
        if code in ("AccessDeniedException", "AccessDenied"):
            return (f"AccessDenied calling AWS ({code}); the credentials need states:ListExecutions, "
                    "states:DescribeExecution and states:GetExecutionHistory")
        if code in ("ThrottlingException", "TooManyRequestsException"):
            return "AWS is throttling requests"
        return f"AWS request failed ({code})"
    return "Could not call AWS; check your local credentials and AWS_REGION"


_arn_cache = None


def resolve_state_machine_arn():
    """STATE_MACHINE_ARN if set, else build it from the account in your credentials.
    Cached on success; retried on failure. sts:GetCallerIdentity needs no IAM grant."""
    global _arn_cache
    if _arn_cache:
        return _arn_cache
    arn = os.environ.get("STATE_MACHINE_ARN", "").strip()
    if not arn:
        account = sts.get_caller_identity()["Account"]
        arn = f"arn:aws:states:{REGION}:{account}:stateMachine:{STATE_MACHINE_NAME}"
    _arn_cache = arn
    return arn


def order_id_from_input(raw):
    """The orderId from an execution's input, or None if it isn't a valid one."""
    if not isinstance(raw, str) or len(raw) > MAX_INPUT_CHARS:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    order_id = value.get("orderId") if isinstance(value, dict) else None
    if isinstance(order_id, str) and ORDER_ID_RE.fullmatch(order_id):
        return order_id
    return None


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _details(ev):
    t = ev["type"]
    return ev.get(t[0].lower() + t[1:] + "EventDetails") or {}


def analyze_history(events, status):
    """Map history events to the four boxes and a plain-language timeline."""
    state = {key: "pending" for key, _ in BOXES}
    timeline = []
    current = None
    submitted = False
    entered_at = None
    timed_out = False

    def note(ev, text):
        timeline.append({"ts": _iso(ev["timestamp"]), "text": text})

    for ev in events:
        t = ev.get("type")
        if t == "ExecutionStarted":
            note(ev, "Execution started")
        elif t in STATE_ENTERED:
            name = ev.get("stateEnteredEventDetails", {}).get("name")
            if name in state:
                current = name
                if name == "ManagerApproval":
                    state[name] = "waiting"
                    entered_at = ev["timestamp"]
                    note(ev, "Approval step reached")
                elif name == "ProcessRefund":
                    note(ev, "Processing refund")
        elif t in STATE_EXITED:
            name = ev.get("stateExitedEventDetails", {}).get("name")
            if name in state:
                state[name] = "done"
                text = {"ValidateOrder": "Order validated", "FraudCheck": "Fraud check passed",
                        "ProcessRefund": "Refund processed (status set to REFUNDED)"}.get(name)
                if text:
                    note(ev, text)
        elif t == "TaskSubmitted" and current == "ManagerApproval":
            submitted = True
            note(ev, "Task token stored in DynamoDB (not shown); waiting for a manager")
        elif t == "TaskSucceeded" and current == "ManagerApproval":
            submitted = False
            note(ev, "Released by manager")
        elif t in TASK_FAILURES or t in EXEC_FAILURES:
            d = _details(ev)
            error = d.get("error") or ""
            timeout = t in ("TaskTimedOut", "ExecutionTimedOut") or error == "States.Timeout"
            timed_out = timed_out or timeout
            if current and state[current] in ("waiting", "pending"):
                state[current] = "timedout" if timeout else "failed"
            where = LABELS.get(current, "Execution") if t in TASK_FAILURES else "Execution"
            if timeout:
                note(ev, f"{where} timed out")
            else:
                detail = _clean(f"{error} {d.get('cause') or ''}")
                note(ev, f"{where} {'aborted' if t == 'ExecutionAborted' else 'failed'}"
                         + (f": {detail}" if detail else ""))
        elif t == "ExecutionSucceeded":
            state["ProcessRefund"] = "done"
            note(ev, "Execution succeeded")

    if status != "RUNNING":
        for key in state:
            if state[key] == "waiting":
                state[key] = "timedout" if (timed_out or status == "TIMED_OUT") else "failed"
    return {
        "boxes": state, "timeline": timeline, "submitted": submitted,
        "timedOut": timed_out, "enteredAt": entered_at,
    }


def fetch_history(arn):
    """All history events for one execution (paged, capped, no execution data)."""
    events, token = [], None
    for _ in range(MAX_HISTORY_PAGES):
        kwargs = {"executionArn": arn, "maxResults": 1000, "includeExecutionData": False}
        if token:
            kwargs["nextToken"] = token
        page = sfn.get_execution_history(**kwargs)
        events.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token:
            break
    return events


_lock = threading.Lock()
_order_ids = collections.OrderedDict()    # executionArn -> orderId (input never changes)
_finished = collections.OrderedDict()     # executionArn -> analysis (finished = immutable)
_snapshot = {"ts": 0.0, "payload": None, "good": None}


def _remember(cache, key, value):
    cache[key] = value
    while len(cache) > (CACHE_LIMIT * 4 if cache is _order_ids else CACHE_LIMIT):
        cache.popitem(last=False)


def build_view(now):
    """Call Step Functions and return the /executions payload (caller holds the lock)."""
    arn = resolve_state_machine_arn()
    listed = sfn.list_executions(stateMachineArn=arn, maxResults=MAX_EXECUTIONS).get("executions", [])
    listed.sort(key=lambda e: e["startDate"], reverse=True)
    out, waiting_orders = [], set()
    for ex in listed:
        ex_arn, status = ex["executionArn"], ex["status"]
        if ex_arn not in _order_ids:
            raw = sfn.describe_execution(executionArn=ex_arn).get("input")
            _remember(_order_ids, ex_arn, order_id_from_input(raw))
        order_id = _order_ids[ex_arn]
        analysis = _finished.get(ex_arn)
        if analysis is None:
            analysis = analyze_history(fetch_history(ex_arn), status)
            if status in FINISHED:
                _remember(_finished, ex_arn, analysis)
        shown = "TIMED_OUT" if status == "FAILED" and analysis["timedOut"] else status
        boxes = analysis["boxes"]
        waiting = status == "RUNNING" and boxes["ManagerApproval"] == "waiting"
        can_approve = (waiting and analysis["submitted"] and order_id is not None
                       and order_id not in waiting_orders)   # newest waiting run owns the token
        if waiting and order_id is not None:
            waiting_orders.add(order_id)
        start, stop = ex["startDate"], ex.get("stopDate")
        end = stop or now
        timeout_at = None
        if waiting and analysis["enteredAt"]:
            timeout_at = datetime.fromtimestamp(
                analysis["enteredAt"].timestamp() + APPROVAL_TIMEOUT_SECONDS, timezone.utc).isoformat()
        out.append({
            "name": ex.get("name", ""), "orderId": order_id, "status": shown,
            "startDate": _iso(start), "stopDate": _iso(stop),
            "elapsedSeconds": max(0, int((end - start).total_seconds())),
            "timeoutAt": timeout_at, "canApprove": can_approve,
            "boxes": [{"key": k, "label": l, "state": boxes[k]} for k, l in BOXES],
            "timeline": analysis["timeline"],
        })
    return {"serverNow": now.isoformat(), "region": REGION, "executions": out}


def get_snapshot():
    """The /executions payload, cached for SNAPSHOT_TTL seconds across all callers."""
    with _lock:
        if time.monotonic() - _snapshot["ts"] < SNAPSHOT_TTL and _snapshot["payload"] is not None:
            return _snapshot["payload"]
        try:
            payload = build_view(datetime.now(timezone.utc))
            _snapshot["good"] = payload
        except (ClientError, BotoCoreError) as e:
            line = aws_error_line(e)
            if _snapshot["good"] is not None:
                payload = dict(_snapshot["good"], warning=f"{line}; showing the last data")
            else:
                payload = {"serverNow": datetime.now(timezone.utc).isoformat(), "region": REGION,
                           "executions": [], "error": line}
        _snapshot["ts"] = time.monotonic()
        _snapshot["payload"] = payload
        return payload


def invalidate_snapshot():
    with _lock:
        _snapshot["ts"] = 0.0


# ---------------------------------------------------------------------------
# The page. Plain HTML + CSS + JS; it polls /executions every 2s and POSTs
# /approve. Everything from AWS is rendered with textContent, never innerHTML.
# ---------------------------------------------------------------------------
STYLE = r"""
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #f2f3f3; color: #16191f; }
  header { background: #232f3e; color: #fff; padding: 16px 24px; font-size: 20px; font-weight: 600; }
  main { padding: 24px; max-width: 960px; margin: 0 auto; }
  #banner { background: #fff3cd; color: #7a5b00; border: 1px solid #e0c36a; border-radius: 6px;
            padding: 8px 12px; margin-bottom: 12px; }
  .card { background: #fff; border: 1px solid #d5dbdb; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }
  .head { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
  .order { font-size: 18px; font-weight: 600; }
  .badge { font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 12px; background: #eaeded; color: #414d5c; }
  .badge.RUNNING { background: #fff3cd; color: #7a5b00; }
  .badge.SUCCEEDED { background: #d4edda; color: #1b5e20; }
  .badge.FAILED, .badge.TIMED_OUT, .badge.ABORTED { background: #f8d7da; color: #8a1c1c; }
  .meta { color: #545b64; font-size: 14px; }
  .strip { display: flex; align-items: stretch; }
  .conn { flex: 0 0 24px; align-self: center; height: 3px; background: #c4cacf; }
  .conn.done { background: #1b8a2f; }
  .box { flex: 1 1 0; min-width: 0; border: 2px solid #c4cacf; background: #f2f3f3; color: #545b64;
         border-radius: 8px; padding: 10px; text-align: center; }
  .box .icon { font-size: 20px; font-weight: 700; line-height: 1.2; }
  .box .name { font-weight: 600; margin-top: 2px; }
  .box .sub { font-size: 13px; margin-top: 2px; }
  .box.done { border-color: #1b8a2f; background: #e8f5e9; color: #1b5e20; }
  .box.waiting { border-color: #ec7211; background: #fff4e0; color: #7a3e00; animation: pulse 1.6s ease-in-out infinite; }
  .box.failed, .box.timedout { border-color: #d13212; background: #fdecea; color: #8a1c1c; }
  @keyframes pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(236, 114, 17, 0.55); }
                     50% { box-shadow: 0 0 0 8px rgba(236, 114, 17, 0); } }
  @media (prefers-reduced-motion: reduce) { .box.waiting { animation: none; } }
  button { background: #1b8a2f; color: #fff; border: 0; border-radius: 6px; margin-top: 8px;
           padding: 8px 18px; font-size: 15px; cursor: pointer; }
  button:disabled { background: #a5b0b5; cursor: default; }
  .notice { color: #8a1c1c; font-size: 13px; margin-top: 6px; }
  details { margin-top: 14px; }
  summary { cursor: pointer; font-weight: 600; color: #414d5c; }
  ul.timeline { list-style: none; margin: 8px 0 0; padding: 0; }
  ul.timeline li { display: flex; gap: 12px; padding: 4px 0; border-top: 1px solid #eaeded; font-size: 14px; }
  ul.timeline .ts { flex: 0 0 90px; color: #545b64; font-variant-numeric: tabular-nums; }
  .empty { color: #6c757d; text-align: center; margin-top: 40px; }
"""

SCRIPT = r"""
'use strict';
var model = null, offsetMs = 0, inFlight = false, lastSig = null, tickers = [];
var approving = {}, notices = {}, openDetails = {};
var STATUS_LABEL = {RUNNING: 'Running', SUCCEEDED: 'Succeeded', FAILED: 'Failed',
                    TIMED_OUT: 'Timed out', ABORTED: 'Aborted'};
var BOX_TEXT = {pending: 'Pending', done: 'Done', waiting: 'Waiting for a manager',
                failed: 'Failed', timedout: 'Timed out'};
var BOX_ICON = {pending: '\u25CB', done: '\u2713', waiting: '\u2026', failed: '\u2715', timedout: '!'};

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function pad(n) { return n < 10 ? '0' + n : '' + n; }
function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec));
  var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h > 0 ? h + ':' + pad(m) + ':' + pad(s) : m + ':' + pad(s);
}
function serverNow() { return Date.now() + offsetMs; }
function emptyMessage(list, text) { list.replaceChildren(el('p', 'empty', text)); }

function card(ex) {
  var c = el('div', 'card');
  var head = el('div', 'head');
  head.append(el('span', 'order', ex.orderId ? 'Order ' + ex.orderId : '(unrecognized input)'),
              el('span', 'badge ' + (STATUS_LABEL[ex.status] ? ex.status : ''), STATUS_LABEL[ex.status] || ex.status));
  var elapsed = el('span', 'meta');
  head.append(elapsed);
  tickers.push(function() {
    var sec = ex.stopDate ? ex.elapsedSeconds : (serverNow() - Date.parse(ex.startDate)) / 1000;
    elapsed.textContent = 'Elapsed ' + fmtDur(sec);
  });
  if (ex.timeoutAt) {
    var cd = el('span', 'meta');
    head.append(cd);
    tickers.push(function() {
      var left = (Date.parse(ex.timeoutAt) - serverNow()) / 1000;
      cd.textContent = left > 0 ? 'Times out in ' + fmtDur(left) : 'Timeout reached';
    });
  }
  c.append(head);

  var strip = el('div', 'strip');
  ex.boxes.forEach(function(b, i) {
    if (i > 0) strip.append(el('div', 'conn' + (ex.boxes[i - 1].state === 'done' ? ' done' : '')));
    var box = el('div', 'box ' + b.state);
    box.append(el('div', 'icon', BOX_ICON[b.state] || ''), el('div', 'name', b.label),
               el('div', 'sub', BOX_TEXT[b.state] || b.state));
    if (b.key === 'ManagerApproval' && b.state === 'waiting') {
      var btn = el('button', '', approving[ex.name] ? 'Approving\u2026' : 'Approve');
      btn.disabled = !ex.canApprove || !!approving[ex.name];
      btn.addEventListener('click', function() { approve(ex); });
      box.append(btn);
      if (notices[ex.name]) box.append(el('div', 'notice', notices[ex.name]));
    }
    strip.append(box);
  });
  c.append(strip);

  var d = el('details');
  d.open = ex.name in openDetails ? openDetails[ex.name] : ex.status === 'RUNNING';
  var sum = el('summary', '', 'Timeline (' + ex.timeline.length + ' events)');
  sum.addEventListener('click', function() { openDetails[ex.name] = !d.open; });
  var ul = el('ul', 'timeline');
  ex.timeline.forEach(function(t) {
    var li = el('li');
    li.append(el('span', 'ts', new Date(t.ts).toLocaleTimeString()), el('span', 'text', t.text));
    ul.append(li);
  });
  d.append(sum, ul);
  c.append(d);
  return c;
}

function render() {
  var list = document.getElementById('list');
  var banner = document.getElementById('banner');
  banner.textContent = model.warning || '';
  banner.hidden = !model.warning;
  if (model.error) { lastSig = null; tickers = []; emptyMessage(list, 'Error: ' + model.error); return; }
  var sig = JSON.stringify([model.executions, approving, notices],
                           function(k, v) { return k === 'elapsedSeconds' ? undefined : v; });
  if (sig === lastSig) return;
  lastSig = sig;
  tickers = [];
  if (model.executions.length === 0) {
    emptyMessage(list, 'No executions yet. Start one from the agent.');
    return;
  }
  list.replaceChildren.apply(list, model.executions.map(card));
  tick();
}
function tick() { tickers.forEach(function(f) { f(); }); }

function poll() {
  if (inFlight) return;
  inFlight = true;
  fetch('/executions', {cache: 'no-store'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      model = data;
      offsetMs = Date.parse(data.serverNow) - Date.now();
      render();
    })
    .catch(function() {
      document.getElementById('banner').textContent = 'Cannot reach the approvals app.';
      document.getElementById('banner').hidden = false;
    })
    .then(function() { inFlight = false; });
}

function approve(ex) {
  approving[ex.name] = true;
  delete notices[ex.name];
  lastSig = null;
  render();
  fetch('/approve', {method: 'POST', headers: {'Content-Type': 'application/json'},
                     body: JSON.stringify({orderId: ex.orderId})})
    .then(function(r) { return r.json(); })
    .then(function(d) { if (d.error) notices[ex.name] = d.error; })
    .catch(function() { notices[ex.name] = 'Cannot reach the approvals app.'; })
    .then(function() { delete approving[ex.name]; lastSig = null; poll(); });
}

poll();
setInterval(poll, 2000);   // live-updates as executions start and progress
setInterval(tick, 1000);   // elapsed time and countdown tick between polls
"""


def _csp_hash(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Refund Approvals</title>
<style>{STYLE}</style>
</head>
<body>
<header>Refund Approvals &mdash; Manager Console</header>
<main><div id="banner" hidden></div><div id="list"></div></main>
<script>{SCRIPT}</script>
</body>
</html>
"""

# Only this page's own inline script and style may run; nothing loads from elsewhere.
PAGE_CSP = ("default-src 'none'; script-src " + _csp_hash(SCRIPT) + "; style-src " + _csp_hash(STYLE)
            + "; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="application/json", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message):
        self._send(status, json.dumps({"error": message}))

    def do_GET(self):
        if not is_local_same_origin(self.headers):
            return self._error(403, "cross-site request refused")
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP})
        elif self.path == "/executions":
            try:
                self._send(200, json.dumps(get_snapshot()))
            except Exception:
                self._send(200, json.dumps({"error": "Unexpected error reading Step Functions data",
                                            "executions": []}))
        else:
            self._error(404, "not found")

    def do_POST(self):
        if not is_local_same_origin(self.headers):
            return self._error(403, "cross-site request refused")
        if self.path != "/approve":
            return self._error(404, "not found")
        self.close_connection = True   # never read an unvetted body twice
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "invalid Content-Length")
        if length < 0:
            return self._error(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            return self._error(413, "request body too large")
        try:
            order_id = json.loads(self.rfile.read(length)).get("orderId")
        except (ValueError, AttributeError):
            return self._error(400, "body must be JSON with an orderId")
        if not isinstance(order_id, str) or not ORDER_ID_RE.fullmatch(order_id):
            return self._error(400, "invalid orderId")
        try:
            # Look up the parked task token and release the workflow.
            item = ddb.get_item(TableName=APPROVALS_TABLE, Key={"orderId": {"S": order_id}}).get("Item")
            if not item or item.get("status", {}).get("S") != "PENDING_APPROVAL" or "taskToken" not in item:
                return self._error(400, "no approval is pending for that order")
            sfn.send_task_success(taskToken=item["taskToken"]["S"],
                                  output=json.dumps({"approved": True}))
            invalidate_snapshot()
            self._send(200, json.dumps({"ok": True}))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "Error")
            self._error(400, f"AWS rejected the approval ({code})")
        except Exception:
            self._error(400, "could not approve; check your AWS credentials and region")

    def log_message(self, *args):
        pass   # keep the console quiet during the demo


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"Refund approvals UI running at {url}  (listening on {BIND_HOST} only; Ctrl-C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()   # open the browser
    server.serve_forever()
