"""
Daily Tracker Bot — WhatsApp → Whisper → GPT → Google Sheets
Soporta múltiples usuarios, sistema de puntos y leaderboard.
"""

import os
import json
import httpx
import tempfile
import secrets
from datetime import datetime, date, timedelta
from fastapi import FastAPI, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────────────────────────────────────
# Variables de entorno
# ─────────────────────────────────────────────────────────────────────────────
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
GOOGLE_CREDS_JSON  = os.environ["GOOGLE_CREDS_JSON"]
SPREADSHEET_ID     = os.environ["SPREADSHEET_ID"]
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "tracker123")

# JSON con los números autorizados y sus nombres
# Ejemplo: {"whatsapp:+5491112345678": "Nico", "whatsapp:+5491187654321": "Fede"}
USUARIOS: dict = json.loads(os.environ.get("USUARIOS_JSON", "{}"))

# ─────────────────────────────────────────────────────────────────────────────
# Sistema de puntos — editá estos valores para personalizar
# ─────────────────────────────────────────────────────────────────────────────
PUNTOS = {
    "suenio_por_hora":   1,
    "suenio_max_horas":  8,
    "gym_base":          3,
    "calidad_bien":      2,
    "calidad_normal":    1,
    "calidad_mal":       0,
    "estudio_por_hora":  1.5,
    "estudio_max_horas": 6,
    "trabajo_por_hora":  1,
    "trabajo_max_horas": 8,
    "comida_por_comida": 1,
    "comidas_max":       4,
    "lectura_base":      2,
}


def calcular_puntos(data: dict) -> float:
    pts = 0.0
    horas_suenio = float(data.get("horas_suenio") or 0)
    pts += min(horas_suenio, PUNTOS["suenio_max_horas"]) * PUNTOS["suenio_por_hora"]
    if data.get("gym"):
        pts += PUNTOS["gym_base"]
        calidad_gym = data.get("calidad_entrenamiento", "")
        pts += PUNTOS.get(f"calidad_{calidad_gym}", 0)
    horas_estudio = float(data.get("horas_estudio") or 0)
    pts += min(horas_estudio, PUNTOS["estudio_max_horas"]) * PUNTOS["estudio_por_hora"]
    horas_trabajo = float(data.get("horas_trabajo") or 0)
    pts += min(horas_trabajo, PUNTOS["trabajo_max_horas"]) * PUNTOS["trabajo_por_hora"]
    if data.get("trabaje"):
        calidad_trab = data.get("calidad_trabajo", "")
        pts += PUNTOS.get(f"calidad_{calidad_trab}", 0)
    comidas = int(data.get("comidas") or 0)
    pts += min(comidas, PUNTOS["comidas_max"]) * PUNTOS["comida_por_comida"]
    if data.get("lei"):
        pts += PUNTOS["lectura_base"]
    return round(pts, 1)


app = FastAPI(title="Daily Tracker Bot")
security = HTTPBasic()
openai_client = OpenAI(api_key=OPENAI_API_KEY)

SHEET_HEADERS = [
    "Fecha", "Usuario", "Horas sueño", "Gym", "Calidad entreno",
    "Estudié", "Horas estudio", "Trabajé", "Horas trabajo", "Calidad trabajo",
    "Comidas", "Leí", "Puntos", "Notas", "Transcripción", "Timestamp"
]


def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet("Registros")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Registros", rows=2000, cols=len(SHEET_HEADERS))
        ws.append_row(SHEET_HEADERS)
    return ws


async def transcribe_audio(media_url: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            media_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            follow_redirects=True,
        )
        resp.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(resp.content)
        tmp_path = f.name
    with open(tmp_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="es",
        )
    os.unlink(tmp_path)
    return transcript.text


EXTRACTION_PROMPT = """
Sos un asistente que extrae datos de un registro diario personal en español rioplatense.
A partir del texto, extraé los datos en JSON con exactamente estos campos:

{{
  "fecha": "YYYY-MM-DD (hoy si no se menciona)",
  "horas_suenio": número decimal o null,
  "gym": true/false,
  "calidad_entrenamiento": "bien"|"normal"|"mal"|null,
  "estudie": true/false,
  "horas_estudio": número decimal o null,
  "trabaje": true/false,
  "horas_trabajo": número decimal o null,
  "calidad_trabajo": "bien"|"normal"|"mal"|null,
  "comidas": número entero o null,
  "lei": true/false (¿leyó algo? libro, artículos, etc.),
  "notas": "texto extra relevante" o null
}}

Fecha de hoy: {today}

Texto:
\"\"\"{text}\"\"\"

Solo el JSON, sin markdown.
"""


def extract_data(transcription: str) -> dict:
    today = date.today().isoformat()
    prompt = EXTRACTION_PROMPT.format(text=transcription, today=today)
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    if not data.get("fecha"):
        data["fecha"] = today
    return data


def bool_str(v):
    if v is True:  return "Sí"
    if v is False: return "No"
    return ""


def save_to_sheet(data: dict, transcription: str, usuario: str, puntos: float):
    ws = get_sheet()
    fecha = data.get("fecha", date.today().isoformat())
    row = [
        fecha, usuario,
        data.get("horas_suenio", ""),
        bool_str(data.get("gym")),
        data.get("calidad_entrenamiento", "") or "",
        bool_str(data.get("estudie")),
        data.get("horas_estudio", "") or "",
        bool_str(data.get("trabaje")),
        data.get("horas_trabajo", "") or "",
        data.get("calidad_trabajo", "") or "",
        data.get("comidas", "") or "",
        bool_str(data.get("lei")),
        puntos,
        data.get("notas", "") or "",
        transcription,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]
    all_rows = ws.get_all_values()
    for i, existing in enumerate(all_rows[1:], start=2):
        if len(existing) >= 2 and existing[0] == fecha and existing[1] == usuario:
            ws.update(f"A{i}:P{i}", [row])
            return
    ws.append_row(row)


@app.post("/webhook")
async def webhook(
    From: str = Form(""),
    Body: str = Form(""),
    MediaUrl0: str = Form(None),
    MediaContentType0: str = Form(None),
    NumMedia: str = Form("0"),
):
    twiml = MessagingResponse()
    if USUARIOS and From not in USUARIOS:
        twiml.message("❌ Tu número no está autorizado. Pedile al admin que te agregue.")
        return HTMLResponse(content=str(twiml), media_type="application/xml")
    usuario = USUARIOS.get(From, From)
    try:
        if int(NumMedia) > 0 and MediaContentType0 and "audio" in MediaContentType0:
            transcription = await transcribe_audio(MediaUrl0)
        elif Body.strip():
            transcription = Body.strip()
        else:
            twiml.message("🎙️ Mandame un audio o texto con tu registro del día.")
            return HTMLResponse(content=str(twiml), media_type="application/xml")
        data   = extract_data(transcription)
        puntos = calcular_puntos(data)
        save_to_sheet(data, transcription, usuario, puntos)
        gym_txt   = "✅ Gym" if data.get("gym") else "❌ Sin gym"
        study_txt = "✅ Estudié" if data.get("estudie") else "❌ No estudié"
        work_txt  = "✅ Trabajé" if data.get("trabaje") else "❌ No trabajé"
        lei_txt   = "📖 Leí" if data.get("lei") else ""
        lines = [
            f"🏆 *+{puntos} pts — Registro del {data.get('fecha')} guardado*\n",
            f"😴 Sueño: {data.get('horas_suenio') or '?'}h",
            gym_txt + (f" ({data.get('calidad_entrenamiento')})" if data.get("calidad_entrenamiento") else ""),
            study_txt + (f" — {data.get('horas_estudio')}h" if data.get("horas_estudio") else ""),
            work_txt + (f" ({data.get('calidad_trabajo')})" if data.get("calidad_trabajo") else ""),
            f"🍽️ Comidas: {data.get('comidas') or '?'}",
        ]
        if lei_txt: lines.append(lei_txt)
        if data.get("notas"): lines.append(f"📝 {data['notas']}")
        twiml.message("\n".join(lines))
    except Exception as e:
        twiml.message(f"⚠️ Error: {str(e)[:200]}")
    return HTMLResponse(content=str(twiml), media_type="application/xml")


def verify_password(credentials: HTTPBasicCredentials = Depends(security)):
    if not secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD):
        raise HTTPException(status_code=401, detail="Contraseña incorrecta",
                            headers={"WWW-Authenticate": "Basic"})
    return credentials


@app.get("/api/data")
def get_data(_=Depends(verify_password)):
    ws = get_sheet()
    return JSONResponse(content=ws.get_all_records())


@app.get("/api/leaderboard")
def get_leaderboard(_=Depends(verify_password)):
    ws = get_sheet()
    rows = ws.get_all_records()
    today = date.today()
    def pts(r):
        try: return float(r.get("Puntos") or 0)
        except: return 0.0
    def in_range(r, days):
        try:
            d = date.fromisoformat(r.get("Fecha", ""))
            return d >= today - timedelta(days=days)
        except: return False
    usuarios_set = {r["Usuario"] for r in rows if r.get("Usuario")}
    board = []
    for u in usuarios_set:
        user_rows = [r for r in rows if r.get("Usuario") == u]
        board.append({
            "usuario":    u,
            "total":      round(sum(pts(r) for r in user_rows), 1),
            "esta_semana": round(sum(pts(r) for r in user_rows if in_range(r, 7)), 1),
            "este_mes":   round(sum(pts(r) for r in user_rows if in_range(r, 30)), 1),
            "este_anio":  round(sum(pts(r) for r in user_rows if in_range(r, 365)), 1),
            "dias":       len(user_rows),
            "mejor_dia":  max((pts(r) for r in user_rows), default=0),
        })
    board.sort(key=lambda x: x["total"], reverse=True)
    return JSONResponse(content=board)


@app.get("/api/config")
def get_config(_=Depends(verify_password)):
    max_pts = (
        PUNTOS["suenio_max_horas"] * PUNTOS["suenio_por_hora"] +
        PUNTOS["gym_base"] + PUNTOS["calidad_bien"] +
        PUNTOS["estudio_max_horas"] * PUNTOS["estudio_por_hora"] +
        PUNTOS["trabajo_max_horas"] * PUNTOS["trabajo_por_hora"] + PUNTOS["calidad_bien"] +
        PUNTOS["comidas_max"] * PUNTOS["comida_por_comida"] +
        PUNTOS["lectura_base"]
    )
    return JSONResponse(content={"puntos": PUNTOS, "max_por_dia": max_pts})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(_=Depends(verify_password)):
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0c0c10">
<title>Daily Tracker</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overscroll-behavior:none}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0c0c10;color:#e8e8f0;
     padding-bottom:calc(64px + env(safe-area-inset-bottom))}
header{position:sticky;top:0;z-index:10;
       background:#12121cee;backdrop-filter:blur(12px);
       border-bottom:1px solid #1e1e2e;
       padding:.75rem 1rem;display:flex;align-items:center;gap:.75rem}
header h1{font-size:1.1rem;font-weight:700;flex:1}
.user-pill{background:#1e1e30;border-radius:20px;padding:.25rem .7rem;
           display:flex;align-items:center;gap:.4rem}
.user-avatar{width:22px;height:22px;border-radius:50%;
             background:#534AB7;display:flex;align-items:center;justify-content:center;
             font-size:.65rem;color:#EEEDFE;font-weight:600}
.bottom-nav{position:fixed;bottom:0;left:0;right:0;z-index:10;
            background:#12121cee;backdrop-filter:blur(12px);
            border-top:1px solid #1e1e2e;
            display:grid;grid-template-columns:repeat(4,1fr);
            padding:.5rem .25rem calc(.5rem + env(safe-area-inset-bottom))}
.nav-item{display:flex;flex-direction:column;align-items:center;gap:2px;
          cursor:pointer;padding:.25rem;border:none;background:none;color:#444}
.nav-item .nav-icon{font-size:1.3rem;line-height:1}
.nav-item .nav-lbl{font-size:.65rem;font-weight:600}
.nav-item.active{color:#8b7fff}
main{padding:.75rem .85rem 0}
.section{display:none}.section.active{display:block}
.user-bar{display:flex;gap:.4rem;margin-bottom:.85rem;flex-wrap:wrap}
.user-btn{padding:.35rem .85rem;border-radius:20px;cursor:pointer;font-size:.8rem;
          font-weight:600;border:1.5px solid #2a2a3e;background:transparent;color:#666}
.user-btn.active{border-color:#8b7fff;background:#1a1630;color:#c0b8ff}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:.55rem;margin-bottom:.85rem}
.stat-card{background:#13131e;border:1px solid #21213a;border-radius:12px;
           padding:.85rem .75rem;text-align:center}
.stat-card .val{font-size:1.7rem;font-weight:700;color:#8b7fff;line-height:1.1}
.stat-card .lbl{font-size:.72rem;color:#666;margin-top:.2rem}
.card{background:#13131e;border:1px solid #21213a;border-radius:14px;padding:1rem;margin-bottom:.75rem}
.card-title{font-size:.72rem;font-weight:600;color:#888;
            letter-spacing:.04em;text-transform:uppercase;margin-bottom:.85rem}
.hz-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:.85rem}
.hz-row .card-title{margin:0}
.hz-btns{display:flex;gap:.3rem}
.hz-btn{padding:.28rem .7rem;border-radius:20px;cursor:pointer;font-size:.72rem;
        font-weight:600;border:1.5px solid #2a2a3e;background:transparent;color:#666}
.hz-btn.active{border-color:#8b7fff;background:#1a1630;color:#c0b8ff}
.metric-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.5rem;margin-bottom:.75rem}
.metric-card{background:#13131e;border:1.5px solid #21213a;border-radius:12px;
             padding:.65rem .45rem;text-align:center;cursor:pointer;
             transition:border-color .12s}
.metric-card .mc-icon{font-size:1.15rem;line-height:1.2}
.metric-card .mc-val{font-size:1.25rem;font-weight:700;line-height:1.1;margin:.15rem 0}
.metric-card .mc-lbl{font-size:.65rem;color:#666}
.metric-card .mc-sub{font-size:.6rem;color:#444}
.drill-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:.85rem}
.drill-title{font-size:.82rem;font-weight:600;color:#c0b8ff}
.drill-close{background:none;border:none;color:#555;cursor:pointer;font-size:1.1rem;padding:.2rem .4rem}
.lb-period{display:flex;gap:.35rem;margin-bottom:.85rem;overflow-x:auto;padding-bottom:2px}
.lb-period::-webkit-scrollbar{display:none}
.period-btn{padding:.3rem .8rem;border-radius:20px;cursor:pointer;font-size:.75rem;
            font-weight:600;border:1.5px solid #2a2a3e;background:transparent;color:#666;white-space:nowrap}
.period-btn.active{border-color:#8b7fff;background:#1a1630;color:#c0b8ff}
.lb-row{display:flex;align-items:center;gap:.75rem;
        background:#13131e;border:1px solid #21213a;border-radius:12px;
        padding:.8rem .9rem;margin-bottom:.55rem}
.lb-rank{font-size:1.2rem;min-width:1.8rem;text-align:center}
.lb-name{font-weight:600;font-size:.9rem;flex:1}
.lb-stats{display:flex;flex-direction:column;align-items:flex-end;gap:.1rem}
.lb-main{color:#8b7fff;font-size:1.1rem;font-weight:700}
.lb-sub{color:#555;font-size:.7rem}
.cmp-grid{display:grid;grid-template-columns:1fr 1fr;gap:.55rem;margin-bottom:.75rem}
.cmp-card{background:#13131e;border:1px solid #21213a;border-radius:12px;padding:.85rem .7rem;text-align:center}
.cmp-card h3{font-size:.68rem;color:#555;margin-bottom:.4rem;text-transform:uppercase;letter-spacing:.04em}
.cmp-card .main-val{font-size:1.5rem;font-weight:700;color:#8b7fff}
.cmp-card .vs{font-size:.72rem;color:#444;margin-top:.25rem}
.up{color:#4ade80}.down{color:#f87171}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:.78rem}
th{color:#555;font-weight:500;padding:.45rem .6rem;border-bottom:1px solid #1e1e2e;white-space:nowrap;text-align:left}
td{padding:.6rem .6rem;border-bottom:1px solid #18182a;white-space:nowrap}
tr:last-child td{border-bottom:none}
.pts{font-weight:700;color:#8b7fff}
.badge{display:inline-block;padding:.15rem .5rem;border-radius:20px;font-size:.68rem;font-weight:600}
.si{background:#152615;color:#4ade80}.no{background:#231515;color:#f87171}
.bien{background:#122030;color:#60a5fa}.normal{background:#282012;color:#fbbf24}.mal{background:#231515;color:#f87171}
.tip{color:#444;font-size:.72rem;margin-bottom:.6rem;padding-left:.1rem}
.empty{text-align:center;padding:2rem;color:#444;font-size:.85rem}
</style>
</head>
<body>
<header>
  <span style="font-size:1.4rem">🏆</span>
  <h1>Daily Tracker</h1>
  <div class="user-pill" id="hdr-user">
    <div class="user-avatar" id="hdr-avatar">?</div>
    <span style="color:#9a9ab0;font-size:.78rem" id="hdr-name">cargando</span>
  </div>
</header>
<main>
  <div class="section active" id="sec-personal">
    <div class="user-bar" id="user-bar-personal"></div>
    <div class="stats-grid" id="stats-cards"></div>
    <div class="card">
      <div class="hz-row">
        <div class="card-title">Puntos</div>
        <div class="hz-btns">
          <button class="hz-btn active" onclick="setHorizon('dias',this)">Días</button>
          <button class="hz-btn" onclick="setHorizon('semanas',this)">Sem</button>
          <button class="hz-btn" onclick="setHorizon('meses',this)">Mes</button>
        </div>
      </div>
      <canvas id="main-pts-chart" style="max-height:200px"></canvas>
    </div>
    <p class="tip">💡 Tocá una métrica para ver el detalle</p>
    <div class="metric-grid" id="metric-cards"></div>
    <div id="drill-card" class="card" style="display:none">
      <div class="drill-header">
        <span class="drill-title" id="drill-title"></span>
        <button class="drill-close" onclick="closeDrill()">✕</button>
      </div>
      <canvas id="drill-chart" style="max-height:180px"></canvas>
    </div>
  </div>
  <div class="section" id="sec-leaderboard">
    <div class="lb-period" id="lb-period-btns">
      <button class="period-btn active" onclick="setPeriod('esta_semana',this)">Esta semana</button>
      <button class="period-btn" onclick="setPeriod('este_mes',this)">Este mes</button>
      <button class="period-btn" onclick="setPeriod('este_anio',this)">Este año</button>
      <button class="period-btn" onclick="setPeriod('total',this)">Histórico</button>
    </div>
    <div id="lb-grid"></div>
  </div>
  <div class="section" id="sec-comparar">
    <div class="user-bar" id="user-bar-compare"></div>
    <div class="cmp-grid" id="cmp-cards"></div>
    <div class="card">
      <div class="card-title">Puntos acumulados — 30 días</div>
      <canvas id="cmp-chart" style="max-height:200px"></canvas>
    </div>
  </div>
  <div class="section" id="sec-historial">
    <div class="user-bar" id="user-bar-hist"></div>
    <div class="card">
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Fecha</th><th>Pts</th><th>Sueño</th><th>Gym</th><th>Estudio</th><th>Trabajo</th><th>Comidas</th><th>Leí</th><th>Notas</th></tr></thead>
          <tbody id="hist-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</main>
<nav class="bottom-nav">
  <button class="nav-item active" onclick="showTab('personal',this)">
    <span class="nav-icon">📊</span><span class="nav-lbl">Tracker</span>
  </button>
  <button class="nav-item" onclick="showTab('leaderboard',this)">
    <span class="nav-icon">🏅</span><span class="nav-lbl">Ranking</span>
  </button>
  <button class="nav-item" onclick="showTab('comparar',this)">
    <span class="nav-icon">📈</span><span class="nav-lbl">Comparar</span>
  </button>
  <button class="nav-item" onclick="showTab('historial',this)">
    <span class="nav-icon">📋</span><span class="nav-lbl">Historial</span>
  </button>
</nav>
<script>
let allRows=[],leaderboard=[],config={};
let activeUser=null,compareUser=null,lbPeriod='esta_semana';
let horizon='dias',activeMetric=null;

async function init(){
  const [dr,lr,cr]=await Promise.all([
    fetch('/api/data').then(r=>r.json()),
    fetch('/api/leaderboard').then(r=>r.json()),
    fetch('/api/config').then(r=>r.json()),
  ]);
  allRows=dr.sort((a,b)=>b['Fecha'].localeCompare(a['Fecha']));
  leaderboard=lr;config=cr;
  const users=[...new Set(allRows.map(r=>r['Usuario']).filter(Boolean))];
  activeUser=users[0]||null;
  compareUser=users[1]||users[0]||null;
  if(activeUser){
    document.getElementById('hdr-avatar').textContent=activeUser[0].toUpperCase();
    document.getElementById('hdr-name').textContent=activeUser;
  }
  buildUserBars(users);
  renderPersonal();renderLeaderboard();renderCompare();renderHistorial();
}

function showTab(name,btn){
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('sec-'+name).classList.add('active');
  window.scrollTo({top:0,behavior:'smooth'});
}

function buildUserBars(users){
  ['personal','compare','hist'].forEach(id=>{
    const bar=document.getElementById('user-bar-'+id);
    if(!bar)return;
    bar.innerHTML=users.map(u=>`<button class="user-btn ${u===activeUser?'active':''}" onclick="selectUser('${u}','${id}')">${u}</button>`).join('');
  });
}
function selectUser(u,section){
  if(section==='compare') compareUser=u;
  else{activeUser=u;document.getElementById('hdr-avatar').textContent=u[0].toUpperCase();document.getElementById('hdr-name').textContent=u;}
  document.querySelectorAll(`#user-bar-${section} .user-btn`).forEach(b=>{b.classList.toggle('active',b.textContent===u);});
  if(section==='personal') renderPersonal();
  else if(section==='compare') renderCompare();
  else renderHistorial();
}

const userRows=u=>allRows.filter(r=>r['Usuario']===u);
const pts=r=>parseFloat(r['Puntos']||0);
const daysAgo=n=>{const d=new Date();d.setDate(d.getDate()-n);return d.toISOString().slice(0,10);};
const inRange=(r,days)=>r['Fecha']>=daysAgo(days);
const weekKey=d=>{const dt=new Date(d+'T12:00:00');dt.setDate(dt.getDate()-(dt.getDay()||7)+1);return dt.toISOString().slice(0,10);};
const monthKey=d=>d.slice(0,7);

function badge(v){
  if(v==='Sí') return `<span class="badge si">Sí</span>`;
  if(v==='No') return `<span class="badge no">No</span>`;
  return '—';
}

function mkChart(id,type,data,opts={}){
  const ctx=document.getElementById(id);
  if(!ctx)return null;
  const ex=Chart.getChart(ctx);if(ex)ex.destroy();
  return new Chart(ctx,{type,data,options:{
    plugins:{legend:{display:opts.legend??false}},
    scales:{x:{ticks:{color:'#555',maxTicksLimit:8},grid:{color:'#1e1e2e'}},
            y:{ticks:{color:'#555'},grid:{color:'#1e1e2e'},min:0,...(opts.yOpts||{})}},
    ...opts.extra,
  }});
}

const PURPLE='rgba(139,127,255,';
const TEAL='rgba(52,211,153,';
const COLORS=['#8b7fff','#34d399','#f59e0b','#f87171','#60a5fa','#e879f9'];

function aggregateData(rows,getValue,reduce='sum'){
  if(horizon==='dias'){
    const days=Array.from({length:30},(_,i)=>daysAgo(29-i));
    return{labels:days.map(d=>d.slice(5)),data:days.map(d=>{const r=rows.find(x=>x['Fecha']===d);return r?(getValue(r)??null):null;})};
  }
  if(horizon==='semanas'){
    const buckets={};const lbls=[];
    for(let i=11;i>=0;i--){const d=daysAgo(i*7);const k=weekKey(d);if(!buckets[k]){buckets[k]=[];lbls.push(k.slice(5));}}
    rows.forEach(r=>{const k=weekKey(r['Fecha']);if(buckets[k])buckets[k].push(getValue(r)??0);});
    return{labels:lbls,data:Object.values(buckets).map(v=>v.length?(reduce==='avg'?v.reduce((a,b)=>a+b,0)/v.length:v.reduce((a,b)=>a+b,0)):null)};
  }
  const buckets={};const lbls=[];
  for(let i=11;i>=0;i--){const d=new Date();d.setMonth(d.getMonth()-i);const k=d.toISOString().slice(0,7);if(!buckets[k]){buckets[k]=[];lbls.push(k.slice(5));}}
  rows.forEach(r=>{const k=monthKey(r['Fecha']);if(buckets[k])buckets[k].push(getValue(r)??0);});
  return{labels:lbls,data:Object.values(buckets).map(v=>v.length?(reduce==='avg'?v.reduce((a,b)=>a+b,0)/v.length:v.reduce((a,b)=>a+b,0)):null)};
}

const METRICS=[
  {key:'suenio',icon:'😴',label:'Sueño',color:'#8b7fff',getValue:r=>parseFloat(r['Horas sueño'])||null,reduce:'avg',unit:'hs',yMax:12},
  {key:'gym',icon:'🏋️',label:'Gym',color:'#34d399',getValue:r=>r['Gym']==='Sí'?1:0,reduce:'sum',unit:'días'},
  {key:'estudio',icon:'📚',label:'Estudio',color:'#60a5fa',getValue:r=>parseFloat(r['Horas estudio'])||null,reduce:'sum',unit:'hs'},
  {key:'trabajo',icon:'💼',label:'Trabajo',color:'#e879f9',getValue:r=>parseFloat(r['Horas trabajo'])||null,reduce:'sum',unit:'hs'},
  {key:'comidas',icon:'🍽️',label:'Comidas',color:'#f97316',getValue:r=>parseInt(r['Comidas'])||null,reduce:'avg',unit:'com',yMax:6},
  {key:'lectura',icon:'📖',label:'Lectura',color:'#a78bfa',getValue:r=>r['Leí']==='Sí'?1:0,reduce:'sum',unit:'días'},
];

function setHorizon(h,btn){
  horizon=h;
  document.querySelectorAll('.hz-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderMainChart();
  if(activeMetric) renderDrillChart(activeMetric);
}

function renderPersonal(){
  const rows=userRows(activeUser);
  if(!rows.length){document.getElementById('stats-cards').innerHTML='<p class="empty">Sin registros aún.</p>';return;}
  const total=rows.length;
  const totalPts=rows.reduce((s,r)=>s+pts(r),0);
  const avgSleep=(rows.reduce((s,r)=>s+(parseFloat(r['Horas sueño'])||0),0)/total).toFixed(1);
  const gymDays=rows.filter(r=>r['Gym']==='Sí').length;
  const weekPts=rows.filter(r=>inRange(r,7)).reduce((s,r)=>s+pts(r),0).toFixed(1);
  const monthPts=rows.filter(r=>inRange(r,30)).reduce((s,r)=>s+pts(r),0).toFixed(1);
  const best=Math.max(...rows.map(r=>pts(r)));
  const streak=calcStreak(rows);
  document.getElementById('stats-cards').innerHTML=`
    <div class="stat-card"><div class="val">${totalPts.toFixed(0)}</div><div class="lbl">Puntos totales</div></div>
    <div class="stat-card"><div class="val">${weekPts}</div><div class="lbl">Esta semana</div></div>
    <div class="stat-card"><div class="val">${monthPts}</div><div class="lbl">Este mes</div></div>
    <div class="stat-card"><div class="val">${best}</div><div class="lbl">Mejor día</div></div>
    <div class="stat-card"><div class="val">${avgSleep}h</div><div class="lbl">Sueño prom.</div></div>
    <div class="stat-card"><div class="val">${gymDays}</div><div class="lbl">Días gym</div></div>
    <div class="stat-card"><div class="val">${streak}🔥</div><div class="lbl">Racha</div></div>
    <div class="stat-card"><div class="val">${total}</div><div class="lbl">Días registrados</div></div>
  `;
  renderMainChart();
  renderMetricCards(rows);
}

function calcStreak(rows){
  const sorted=[...rows].sort((a,b)=>b['Fecha'].localeCompare(a['Fecha']));
  let streak=0,d=new Date();
  for(const r of sorted){
    const exp=d.toISOString().slice(0,10);
    if(r['Fecha']===exp){streak++;d.setDate(d.getDate()-1);}
    else if(r['Fecha']<exp) break;
  }
  return streak;
}

function renderMainChart(){
  const rows=userRows(activeUser);
  const{labels,data}=aggregateData(rows,r=>pts(r),'sum');
  mkChart('main-pts-chart','bar',{labels,datasets:[{
    data,backgroundColor:data.map(v=>v===null?'transparent':PURPLE+'0.75)'),
    borderColor:data.map(v=>v===null?'transparent':'#8b7fff'),borderRadius:4,
  }]});
}

function renderMetricCards(rows){
  const container=document.getElementById('metric-cards');
  container.innerHTML=METRICS.map(m=>{
    const vals=rows.map(r=>m.getValue(r)).filter(v=>v!==null);
    const total=vals.reduce((a,b)=>a+b,0);
    const avg=vals.length?total/vals.length:0;
    const display=(m.reduce==='avg')?avg.toFixed(1):total.toFixed(0);
    const sublbl=(m.reduce==='avg')?'prom.':'total';
    return`<div class="metric-card" id="mc-${m.key}" onclick="openDrill('${m.key}')"
             style="border-color:${activeMetric===m.key?m.color:'#21213a'}">
      <div class="mc-icon">${m.icon}</div>
      <div class="mc-val" style="color:${m.color}">${display}</div>
      <div class="mc-lbl">${m.label}</div>
      <div class="mc-sub">${sublbl} (${m.unit})</div>
    </div>`;
  }).join('');
}

function openDrill(key){
  activeMetric=key;
  document.querySelectorAll('.metric-card').forEach(c=>c.style.borderColor='#21213a');
  const m=METRICS.find(x=>x.key===key);
  if(m) document.getElementById('mc-'+key).style.borderColor=m.color;
  renderDrillChart(key);
  document.getElementById('drill-card').style.display='block';
  document.getElementById('drill-card').scrollIntoView({behavior:'smooth',block:'nearest'});
}

function closeDrill(){
  activeMetric=null;
  document.getElementById('drill-card').style.display='none';
  document.querySelectorAll('.metric-card').forEach(c=>c.style.borderColor='#21213a');
}

function renderDrillChart(key){
  const m=METRICS.find(x=>x.key===key);if(!m)return;
  const rows=userRows(activeUser);
  const{labels,data}=aggregateData(rows,m.getValue,m.reduce);
  const horizonLbl=horizon==='dias'?'30 días':horizon==='semanas'?'12 semanas':'12 meses';
  document.getElementById('drill-title').textContent=`${m.icon} ${m.label} — ${horizonLbl}`;
  mkChart('drill-chart','line',{labels,datasets:[{
    data,borderColor:m.color,backgroundColor:`${m.color}22`,
    fill:true,tension:.4,pointRadius:2,pointBackgroundColor:m.color,
  }]},{yOpts:m.yMax?{max:m.yMax}:{}});
}

function setPeriod(p,btn){
  lbPeriod=p;
  document.querySelectorAll('.period-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderLeaderboard();
}
function renderLeaderboard(){
  const sorted=[...leaderboard].sort((a,b)=>b[lbPeriod]-a[lbPeriod]);
  const medals=['🥇','🥈','🥉'];
  const lbl={esta_semana:'esta semana',este_mes:'este mes',este_anio:'este año',total:'histórico'};
  document.getElementById('lb-grid').innerHTML=sorted.map((u,i)=>`
    <div class="lb-row">
      <div class="lb-rank">${medals[i]||`#${i+1}`}</div>
      <div><div class="lb-name">${u.usuario}</div>
      <div style="color:#555;font-size:.7rem">${u.dias} días · mejor: ${u.mejor_dia} pts</div></div>
      <div class="lb-stats"><div class="lb-main">${u[lbPeriod]}</div><div class="lb-sub">${lbl[lbPeriod]}</div></div>
    </div>`).join('')||'<p class="empty">Sin datos aún.</p>';
}

function renderCompare(){
  const me=userRows(activeUser);
  const them=userRows(compareUser);
  const label=compareUser===activeUser?'vs antes':`vs ${compareUser}`;
  const metrics=[
    {name:'Esta semana',me:me.filter(r=>inRange(r,7)).reduce((s,r)=>s+pts(r),0),them:them.filter(r=>inRange(r,7)).reduce((s,r)=>s+pts(r),0)},
    {name:'Este mes',me:me.filter(r=>inRange(r,30)).reduce((s,r)=>s+pts(r),0),them:them.filter(r=>inRange(r,30)).reduce((s,r)=>s+pts(r),0)},
    {name:'Total',me:me.reduce((s,r)=>s+pts(r),0),them:them.reduce((s,r)=>s+pts(r),0)},
    {name:'Mejor día',me:Math.max(0,...me.map(r=>pts(r))),them:Math.max(0,...them.map(r=>pts(r)))},
  ];
  document.getElementById('cmp-cards').innerHTML=metrics.map(m=>{
    const d=m.me-m.them;
    const sign=d>0?`<span class="up">+${d.toFixed(1)}</span>`:d<0?`<span class="down">${d.toFixed(1)}</span>`:`<span style="color:#555">empate</span>`;
    return`<div class="cmp-card"><h3>${m.name}</h3><div class="main-val">${m.me.toFixed(1)}</div><div class="vs">${label}: ${m.them.toFixed(1)} · ${sign}</div></div>`;
  }).join('');
  const days30=Array.from({length:30},(_,i)=>daysAgo(29-i));
  function cumPts(rows){let acc=0;return days30.map(d=>{const r=rows.find(x=>x['Fecha']===d);acc+=r?pts(r):0;return parseFloat(acc.toFixed(1));});}
  const datasets=[{label:activeUser,data:cumPts(me),borderColor:COLORS[0],backgroundColor:PURPLE+'0.1)',fill:true,tension:.3}];
  if(compareUser!==activeUser) datasets.push({label:compareUser,data:cumPts(them),borderColor:COLORS[1],backgroundColor:TEAL+'0.1)',fill:true,tension:.3});
  const ex=Chart.getChart(document.getElementById('cmp-chart'));if(ex)ex.destroy();
  new Chart(document.getElementById('cmp-chart'),{type:'line',data:{labels:days30.map(d=>d.slice(5)),datasets},
    options:{plugins:{legend:{display:datasets.length>1,labels:{color:'#888',boxWidth:12,font:{size:11}}}},
             scales:{x:{ticks:{color:'#555',maxTicksLimit:8},grid:{color:'#1e1e2e'}},y:{ticks:{color:'#555'},grid:{color:'#1e1e2e'},min:0}}}});
}

function renderHistorial(){
  const rows=userRows(activeUser);
  if(!rows.length){document.getElementById('hist-tbody').innerHTML='<tr><td colspan="9" class="empty">Sin registros.</td></tr>';return;}
  document.getElementById('hist-tbody').innerHTML=rows.map(r=>`
    <tr><td>${r['Fecha']}</td><td class="pts">${pts(r)}</td>
    <td>${r['Horas sueño']||'—'}h</td><td>${badge(r['Gym'])}</td>
    <td>${r['Horas estudio']||'—'}h</td><td>${r['Horas trabajo']||'—'}h</td>
    <td>${r['Comidas']||'—'}</td><td>${badge(r['Leí'])}</td>
    <td style="max-width:130px;overflow:hidden;text-overflow:ellipsis">${r['Notas']||'—'}</td></tr>`).join('');
}

init().catch(console.error);
</script>
</body>
</html>
"""
