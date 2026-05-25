# -*- coding: utf-8 -*-
"""
공항철도 직통열차 쿠폰 지급 판정기 (웹 버전)
- Flask 백엔드 + 단일 페이지 프론트엔드
- 접근 비밀번호 인증 포함

실행:
    pip install -r requirements.txt
    python app.py
브라우저에서 http://localhost:5000 접속.
"""

from flask import Flask, request, jsonify, render_template_string, session, redirect
from datetime import datetime, timedelta
import urllib.request
import urllib.parse
import ssl
import json
import re
import os

app = Flask(__name__)

# ===================== 설정 =====================
SERVICE_KEY = os.environ.get(
    "AREX_SERVICE_KEY",
    "0a09a47666467a99fafe451730d3bf92af6e9892aa164a06d518638716a54d57",
)
API_URL = "http://apis.data.go.kr/B551177/StatusOfPassengerFlightsOdp/getPassengerArrivalsOdp"
DELAY_THRESHOLD_MIN = 15
GAP_THRESHOLD_MIN = 90

# 접근 비밀번호
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "7321")

# 세션 암호화용 시크릿 (운영 시 환경변수로 바꿔 주세요)
app.secret_key = os.environ.get(
    "FLASK_SECRET",
    "arex-coupon-checker-please-change-this-secret-in-production"
)
app.permanent_session_lifetime = timedelta(days=30)


# ===================== 인증 미들웨어 =====================
@app.before_request
def require_login():
    # 로그인 페이지 자체는 인증 불필요
    if request.endpoint == "login" or request.path == "/login":
        return
    # 인증되지 않은 경우
    if not session.get("authed"):
        # API 요청이면 401 반환 (프론트엔드가 로그인 페이지로 이동시킴)
        if request.path.startswith("/api/"):
            return jsonify({
                "ok": False,
                "message": "세션이 만료되었습니다. 다시 로그인해 주세요.",
                "redirect": "/login",
            }), 401
        # 일반 페이지 요청이면 로그인 페이지로 리다이렉트
        return redirect("/login")


# ===================== API 호출 =====================
def call_api(flight_id):
    params = {
        "serviceKey": SERVICE_KEY,
        "from_time": "0000",
        "to_time": "2400",
        "flight_id": flight_id,
        "lang": "K",
        "type": "json",
    }
    query = urllib.parse.urlencode(params, safe="%")
    url = f"{API_URL}?{query}"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=ctx) as res:
        raw = res.read().decode("utf-8", errors="replace")

    raw_stripped = raw.lstrip()
    if raw_stripped.startswith("{"):
        return json.loads(raw)
    return parse_xml_response(raw)


def parse_xml_response(xml_text):
    result_code_m = re.search(r"<resultCode>(.*?)</resultCode>", xml_text)
    result_msg_m = re.search(r"<resultMsg>(.*?)</resultMsg>", xml_text)
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml_text, re.DOTALL):
        block = m.group(1)
        d = {}
        for tag in re.finditer(r"<(\w+)>(.*?)</\1>", block, re.DOTALL):
            d[tag.group(1)] = tag.group(2).strip()
        items.append(d)
    return {
        "response": {
            "header": {
                "resultCode": result_code_m.group(1) if result_code_m else "",
                "resultMsg": result_msg_m.group(1) if result_msg_m else "",
            },
            "body": {"items": items},
        }
    }


def extract_items(resp):
    try:
        body = resp.get("response", {}).get("body", {})
        items = body.get("items", [])
        if isinstance(items, dict):
            inner = items.get("item", [])
            if isinstance(inner, dict):
                return [inner]
            return inner or []
        if isinstance(items, list):
            return items
        return []
    except Exception:
        return []


def find_matching_flight(items, flight_id):
    flight_id_norm = flight_id.replace(" ", "").upper()
    for it in items:
        fid = str(it.get("flightId") or "").replace(" ", "").upper()
        if fid == flight_id_norm:
            return it
    return None


def fetch_flight(flight_id):
    resp = call_api(flight_id)
    header = resp.get("response", {}).get("header", {})
    result_code = str(header.get("resultCode", ""))
    if result_code and result_code != "00":
        msg = header.get("resultMsg", "알 수 없는 오류")
        raise RuntimeError(f"API 응답 오류 (코드 {result_code}): {msg}")
    items = extract_items(resp)
    if not items:
        return None
    return find_matching_flight(items, flight_id)


# ===================== 시간 처리 =====================
def parse_hhmm(s):
    s = (s or "").strip()
    if not re.fullmatch(r"\d{4}", s):
        return None
    hh, mm = int(s[:2]), int(s[2:])
    if hh == 24 and mm == 0:
        today = datetime.now().date()
        return datetime.combine(today, datetime.min.time()) + timedelta(days=1)
    if hh > 23 or mm > 59:
        return None
    today = datetime.now().date()
    return datetime.combine(today, datetime.strptime(s, "%H%M").time())


# ===================== 판정 로직 =====================
def judge_coupon(flight_id, train_hhmm):
    info = fetch_flight(flight_id)
    if not info:
        return {
            "found": False,
            "message": f"항공편 '{flight_id}' 정보를 당일 도착 목록에서 찾을 수 없습니다. "
                       f"(인천공항 OpenAPI는 당일 운항 정보만 제공합니다.)",
        }

    sched_str = str(info.get("scheduleDateTime") or "").strip()
    est_str = str(info.get("estimatedDateTime") or "").strip()
    remark = str(info.get("remark") or "").strip()

    sched_dt = parse_hhmm(sched_str)
    if sched_dt is None:
        return {"found": False, "message": "항공편 예정 도착시각을 확인할 수 없습니다."}

    est_dt = parse_hhmm(est_str) if est_str else None
    if est_dt and est_dt < sched_dt - timedelta(hours=12):
        est_dt += timedelta(days=1)

    train_dt = parse_hhmm(train_hhmm)
    if train_dt is None:
        return {"found": False, "message": "열차 출발시간은 4자리 숫자(예: 1200)로 입력해 주세요."}

    if train_dt < sched_dt - timedelta(hours=12):
        train_dt += timedelta(days=1)

    gap_min = (train_dt - sched_dt).total_seconds() / 60.0

    if est_dt is None:
        delay_min = 0.0
    else:
        delay_min = (est_dt - sched_dt).total_seconds() / 60.0
        if delay_min < 0:
            delay_min = 0.0

    cond1 = gap_min >= GAP_THRESHOLD_MIN
    cond2 = delay_min >= DELAY_THRESHOLD_MIN

    return {
        "found": True,
        "verdict": "지급" if (cond1 and cond2) else "미지급",
        "flight_id": flight_id.upper(),
        "remark": remark or "-",
        "scheduled": sched_dt.strftime("%H:%M"),
        "estimated": est_dt.strftime("%H:%M") if est_dt else None,
        "train": train_dt.strftime("%H:%M"),
        "gap_min": int(gap_min),
        "delay_min": int(delay_min),
        "cond1": cond1,
        "cond2": cond2,
    }


# ===================== 라우트 =====================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        if password == ACCESS_PASSWORD:
            session.permanent = True
            session["authed"] = True
            return redirect("/")
        error = "비밀번호가 올바르지 않습니다."
    return render_template_string(LOGIN_TEMPLATE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/check")
def api_check():
    flight_id = request.args.get("flight_id", "").strip()
    train = request.args.get("train", "").strip()

    if not flight_id:
        return jsonify({"ok": False, "message": "항공편명을 입력해 주세요."})
    if not re.fullmatch(r"\d{4}", train):
        return jsonify({"ok": False, "message": "열차 출발시간은 4자리 숫자로 입력해 주세요. (예: 1200)"})

    try:
        result = judge_coupon(flight_id, train)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "message": f"API 호출 중 오류가 발생했습니다: {e}"})


# ===================== 로그인 페이지 =====================
LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>접근 인증 · 공항철도 직통열차 쿠폰 판정</title>
<link rel="stylesheet" as="style" crossorigin
      href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f1ece3;
  --paper: #fdfbf7;
  --ink: #1a2332;
  --ink-light: #6b7280;
  --line: #d4cdc0;
  --accent: #b8101e;
  --danger: #b8101e;
  --danger-bg: #fae5e7;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { -webkit-font-smoothing: antialiased; }
body {
  font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  background: var(--bg);
  background-image:
    repeating-linear-gradient(0deg, transparent 0, transparent 28px,
      rgba(26,35,50,0.04) 28px, rgba(26,35,50,0.04) 29px);
  color: var(--ink);
  min-height: 100vh;
  padding: 32px 16px 64px;
  line-height: 1.5;
  display: flex;
  align-items: center;
  justify-content: center;
}
.container { max-width: 420px; width: 100%; margin: 0 auto; }
.header {
  text-align: center;
  margin-bottom: 24px;
  padding-bottom: 22px;
  position: relative;
}
.header::after {
  content: ""; position: absolute; bottom: 0; left: 50%;
  transform: translateX(-50%); width: 56px; height: 3px;
  background: var(--ink);
}
.eyebrow {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px; font-weight: 500;
  letter-spacing: 0.18em;
  color: var(--ink-light);
  margin-bottom: 10px;
  text-transform: uppercase;
}
.eyebrow .dot { color: var(--accent); }
h1 {
  font-size: 22px; font-weight: 800;
  letter-spacing: -0.02em;
  line-height: 1.2;
}
.card {
  background: var(--paper);
  border: 1px solid var(--line);
  padding: 28px 24px;
  box-shadow:
    0 1px 0 rgba(0,0,0,0.03),
    0 8px 24px -16px rgba(26,35,50,0.18);
}
.card-title {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10px; font-weight: 700;
  letter-spacing: 0.2em;
  color: var(--ink-light);
  text-transform: uppercase;
  padding-bottom: 12px;
  margin-bottom: 20px;
  border-bottom: 1px dashed var(--line);
}
label {
  display: block;
  font-size: 13px; font-weight: 600;
  margin-bottom: 8px;
  color: var(--ink);
}
.label-hint {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10px; font-weight: 500;
  color: var(--ink-light);
  margin-left: 6px;
  letter-spacing: 0.05em;
}
input[type="password"] {
  width: 100%;
  padding: 14px 16px;
  border: 1px solid var(--line);
  background: white;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 20px; font-weight: 500;
  letter-spacing: 0.3em;
  color: var(--ink);
  border-radius: 0;
  text-align: center;
  transition: border-color 0.15s, box-shadow 0.15s;
}
input[type="password"]:focus {
  outline: none;
  border-color: var(--ink);
  box-shadow: 0 0 0 3px rgba(26,35,50,0.08);
}
.error {
  margin-top: 12px;
  padding: 10px 14px;
  background: var(--danger-bg);
  color: var(--danger);
  font-size: 13px;
  font-weight: 500;
  text-align: center;
}
button.submit {
  width: 100%;
  padding: 16px;
  margin-top: 20px;
  background: var(--ink);
  color: var(--paper);
  border: none;
  font-family: inherit;
  font-size: 15px; font-weight: 700;
  letter-spacing: 0.05em;
  cursor: pointer;
  transition: background 0.15s, transform 0.05s;
}
button.submit:hover { background: #2a3548; }
button.submit:active { transform: translateY(1px); }
.footer {
  text-align: center;
  margin-top: 28px;
  font-size: 11px;
  color: var(--ink-light);
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  letter-spacing: 0.12em;
}
</style>
</head>
<body>
<div class="container">
  <header class="header">
    <div class="eyebrow">AREX <span class="dot">·</span> RESTRICTED</div>
    <h1>접근 인증</h1>
  </header>

  <form class="card" method="POST" novalidate>
    <div class="card-title">AUTHORIZED PERSONNEL ONLY</div>
    <label for="password">비밀번호 <span class="label-hint">PASSWORD</span></label>
    <input type="password" id="password" name="password" autocomplete="off" autofocus required inputmode="numeric">
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <button type="submit" class="submit">확인</button>
  </form>

  <div class="footer">AREX · STAFF ACCESS ONLY</div>
</div>
</body>
</html>
"""


# ===================== 메인 페이지 =====================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>공항철도 직통열차 쿠폰 판정</title>
<link rel="stylesheet" as="style" crossorigin
      href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f1ece3;
  --paper: #fdfbf7;
  --ink: #1a2332;
  --ink-light: #6b7280;
  --line: #d4cdc0;
  --line-strong: #b8b1a3;
  --accent: #b8101e;
  --success: #1f6b3f;
  --success-bg: #e7f0e9;
  --danger: #b8101e;
  --danger-bg: #fae5e7;
  --warning-bg: #f5efe2;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { -webkit-font-smoothing: antialiased; }
body {
  font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  background: var(--bg);
  background-image:
    repeating-linear-gradient(0deg, transparent 0, transparent 28px,
      rgba(26,35,50,0.04) 28px, rgba(26,35,50,0.04) 29px);
  color: var(--ink);
  min-height: 100vh;
  padding: 32px 16px 64px;
  line-height: 1.5;
}
.container { max-width: 540px; margin: 0 auto; position: relative; }
.logout {
  position: absolute;
  top: 0; right: 0;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px;
  color: var(--ink-light);
  text-decoration: none;
  letter-spacing: 0.1em;
  padding: 4px 10px;
  border: 1px solid var(--line);
}
.logout:hover { background: var(--paper); color: var(--ink); }

.header {
  text-align: center;
  margin-bottom: 28px;
  padding-bottom: 24px;
  position: relative;
}
.header::after {
  content: ""; position: absolute; bottom: 0; left: 50%;
  transform: translateX(-50%); width: 64px; height: 3px;
  background: var(--ink);
}
.eyebrow {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px; font-weight: 500;
  letter-spacing: 0.18em;
  color: var(--ink-light);
  margin-bottom: 10px;
  text-transform: uppercase;
}
.eyebrow .dot { color: var(--accent); }
h1 {
  font-size: 26px; font-weight: 800;
  letter-spacing: -0.025em;
  line-height: 1.2;
}
.card {
  background: var(--paper);
  border: 1px solid var(--line);
  padding: 28px 24px;
  margin-bottom: 16px;
  box-shadow:
    0 1px 0 rgba(0,0,0,0.03),
    0 8px 24px -16px rgba(26,35,50,0.18);
}
.card-title {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10px; font-weight: 700;
  letter-spacing: 0.2em;
  color: var(--ink-light);
  text-transform: uppercase;
  padding-bottom: 12px;
  margin-bottom: 20px;
  border-bottom: 1px dashed var(--line);
}
.form-group { margin-bottom: 18px; }
.form-group:last-of-type { margin-bottom: 24px; }
label {
  display: block;
  font-size: 13px; font-weight: 600;
  margin-bottom: 8px;
  color: var(--ink);
}
.label-hint {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10px; font-weight: 500;
  color: var(--ink-light);
  margin-left: 6px;
  letter-spacing: 0.05em;
}
input[type="text"] {
  width: 100%;
  padding: 14px 16px;
  border: 1px solid var(--line);
  background: white;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 18px; font-weight: 500;
  color: var(--ink);
  border-radius: 0;
  transition: border-color 0.15s, box-shadow 0.15s;
}
input[type="text"]:focus {
  outline: none;
  border-color: var(--ink);
  box-shadow: 0 0 0 3px rgba(26,35,50,0.08);
}
.hint {
  font-size: 12px;
  color: var(--ink-light);
  margin-top: 6px;
}
button.submit {
  width: 100%;
  padding: 16px;
  background: var(--ink);
  color: var(--paper);
  border: none;
  font-family: inherit;
  font-size: 15px; font-weight: 700;
  letter-spacing: 0.05em;
  cursor: pointer;
  transition: background 0.15s, transform 0.05s;
}
button.submit:hover:not(:disabled) { background: #2a3548; }
button.submit:active { transform: translateY(1px); }
button.submit:disabled { opacity: 0.6; cursor: wait; }

.result {
  border: 1px solid var(--line);
  background: var(--paper);
  overflow: hidden;
  display: none;
  animation: slide 0.3s ease-out;
}
.result.show { display: block; }
@keyframes slide {
  from { opacity: 0; transform: translateY(-6px); }
  to { opacity: 1; transform: translateY(0); }
}
.banner {
  padding: 28px 24px;
  text-align: center;
  border-bottom: 1px solid var(--line);
}
.banner .verdict-label {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10px; font-weight: 500;
  letter-spacing: 0.2em;
  opacity: 0.55;
  margin-bottom: 10px;
  text-transform: uppercase;
}
.banner .verdict {
  font-size: 30px; font-weight: 800;
  letter-spacing: -0.02em;
}
.banner.approve { background: var(--success-bg); color: var(--success); }
.banner.deny { background: var(--danger-bg); color: var(--danger); }
.banner.error { background: var(--warning-bg); color: var(--ink); }
.banner.error .verdict { font-size: 16px; font-weight: 600; line-height: 1.5; }

.result-body { padding: 20px 24px 12px; }
.data-row {
  display: flex; justify-content: space-between; align-items: baseline;
  padding: 11px 0;
  border-bottom: 1px dashed var(--line);
  font-size: 14px;
}
.data-row:last-child { border-bottom: none; }
.data-label { color: var(--ink-light); font-weight: 500; }
.data-value {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-weight: 500; color: var(--ink);
  font-size: 15px;
}
.data-value.muted { color: var(--ink-light); }

.checks {
  margin: 16px 24px 24px;
  padding-top: 16px;
  border-top: 2px solid var(--ink);
}
.check-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 7px 0;
  font-size: 13px;
}
.check-text { color: var(--ink); flex: 1; }
.check-text .num {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-weight: 700;
}
.check-mark {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-weight: 700; font-size: 13px;
  padding: 3px 10px;
  margin-left: 12px;
  flex-shrink: 0;
}
.check-mark.pass { background: var(--success); color: var(--success-bg); }
.check-mark.fail { background: var(--danger); color: var(--danger-bg); }

.footer {
  text-align: center;
  margin-top: 32px;
  font-size: 11px;
  color: var(--ink-light);
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  letter-spacing: 0.1em;
}
.footer .pipe { margin: 0 8px; opacity: 0.5; }

@media (max-width: 480px) {
  body { padding: 20px 12px 48px; }
  h1 { font-size: 22px; }
  .card { padding: 24px 18px; }
  .banner { padding: 24px 18px; }
  .banner .verdict { font-size: 26px; }
  .logout { top: -2px; }
}
</style>
</head>
<body>
<div class="container">
  <a href="/logout" class="logout">LOGOUT</a>
  <header class="header">
    <div class="eyebrow">AREX <span class="dot">·</span> COUPON ELIGIBILITY</div>
    <h1>공항철도 직통열차 쿠폰 판정</h1>
  </header>

  <form class="card" id="form" novalidate>
    <div class="card-title">FLIGHT &amp; TRAIN INPUT</div>

    <div class="form-group">
      <label for="flight">항공편명 <span class="label-hint">FLIGHT ID</span></label>
      <input type="text" id="flight" name="flight" placeholder="예: KE036" autocomplete="off" required>
    </div>

    <div class="form-group">
      <label for="train">직통열차 출발시간 <span class="label-hint">HHMM</span></label>
      <input type="text" id="train" name="train" placeholder="1200"
             inputmode="numeric" maxlength="4" required>
      <div class="hint">4자리 숫자로 입력해 주세요. 예) 오후 12시 → 1200</div>
    </div>

    <button type="submit" class="submit" id="submit">확인</button>
  </form>

  <div class="result" id="result"></div>

  <div class="footer">
    SOURCE<span class="pipe">·</span>data.go.kr<span class="pipe">·</span>인천국제공항공사
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const form = $('form');
const flightInput = $('flight');
const trainInput = $('train');
const submitBtn = $('submit');
const resultEl = $('result');

trainInput.addEventListener('input', (e) => {
  e.target.value = e.target.value.replace(/[^0-9]/g, '').slice(0, 4);
});
flightInput.addEventListener('input', (e) => {
  e.target.value = e.target.value.toUpperCase().replace(/\s+/g, '');
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const flight = flightInput.value.trim();
  const train = trainInput.value.trim();
  if (!flight) { flightInput.focus(); return; }
  if (train.length !== 4) {
    renderError('열차 출발시간은 4자리 숫자로 입력해 주세요. (예: 1200)');
    trainInput.focus();
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = '조회 중...';
  resultEl.classList.remove('show');

  try {
    const url = `/api/check?flight_id=${encodeURIComponent(flight)}&train=${encodeURIComponent(train)}`;
    const res = await fetch(url);
    if (res.status === 401) {
      window.location.href = '/login';
      return;
    }
    const data = await res.json();
    render(data);
  } catch (err) {
    renderError('서버 통신 중 오류가 발생했습니다: ' + err.message);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = '확인';
  }
});

function render(data) {
  if (!data.ok) { renderError(data.message); return; }
  const r = data.result;
  if (!r.found) { renderError(r.message); return; }

  const approve = r.verdict === '지급';
  resultEl.innerHTML = `
    <div class="banner ${approve ? 'approve' : 'deny'}">
      <div class="verdict-label">VERDICT</div>
      <div class="verdict">${approve ? '쿠폰 지급' : '쿠폰 지급대상 아님'}</div>
    </div>
    <div class="result-body">
      <div class="data-row">
        <span class="data-label">편명</span>
        <span class="data-value">${escapeHtml(r.flight_id)}</span>
      </div>
      <div class="data-row">
        <span class="data-label">운항상태</span>
        <span class="data-value">${escapeHtml(r.remark)}</span>
      </div>
      <div class="data-row">
        <span class="data-label">계획 도착</span>
        <span class="data-value">${r.scheduled}</span>
      </div>
      <div class="data-row">
        <span class="data-label">변경 도착</span>
        <span class="data-value ${r.estimated ? '' : 'muted'}">${r.estimated || '— 정보 없음'}</span>
      </div>
      <div class="data-row">
        <span class="data-label">열차 출발</span>
        <span class="data-value">${r.train}</span>
      </div>
    </div>
    <div class="checks">
      <div class="check-row">
        <span class="check-text">도착~열차 간격 <span class="num">${r.gap_min}분</span> · 기준 90분 이상</span>
        <span class="check-mark ${r.cond1 ? 'pass' : 'fail'}">${r.cond1 ? 'PASS' : 'FAIL'}</span>
      </div>
      <div class="check-row">
        <span class="check-text">지연 <span class="num">${r.delay_min}분</span> · 기준 15분 이상</span>
        <span class="check-mark ${r.cond2 ? 'pass' : 'fail'}">${r.cond2 ? 'PASS' : 'FAIL'}</span>
      </div>
    </div>
  `;
  resultEl.classList.add('show');
}

function renderError(msg) {
  resultEl.innerHTML = `
    <div class="banner error">
      <div class="verdict-label">CHECK FAILED</div>
      <div class="verdict">${escapeHtml(msg || '오류가 발생했습니다.')}</div>
    </div>
  `;
  resultEl.classList.add('show');
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
