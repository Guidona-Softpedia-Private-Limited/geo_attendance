import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime
import asyncio

app = FastAPI()

# ---------------- UI SETUP ----------------

templates = Jinja2Templates(directory="templates")

ENDPOINTS = [
    "/iclock/cdata.aspx",
    "/iclock/getrequest.aspx",
    "/iclock/registry.aspx",
    "/iclock/devicecmd.aspx"
]

COMMANDS = [
    "INFO",
    "GET ATTLOG",
    "SET OPTION RTLOG=1",
    "SET OPTION PUSH=1",
    "CLEAR ATTLOG",
    "GET OPTION",
    "GET USERINFO",
    "GET BIODATA",
    "GET PICTURE"
]

LOGS = []
ATTENDANCE_DATA = []
COMMAND_QUEUE = []
DEVICE_SN = "Unknown"

def log(msg: str):
    ts = f"{datetime.utcnow().isoformat()}Z - {msg}"
    print(ts)
    LOGS.append(ts)

def log_attendance(msg: str):
    ts = f"{datetime.utcnow().isoformat()}Z - {msg}"
    ATTENDANCE_DATA.append(ts)

async def log_request(request: Request, body: str):
    log("DEVICE REQUEST")
    log(f"  CLIENT   : {request.client.host}")
    log(f"  ENDPOINT : {request.url.path}")
    log(f"  METHOD   : {request.method}")
    log(f"  QUERY    : {dict(request.query_params)}")
    if body and len(body) > 1000:
        log(f"  BODY     : {body[:1000]}... ({len(body)} chars)")
    else:
        log(f"  BODY     : {body if body else '<empty>'}")
    log("-" * 60)

async def auto_send_commands():
    while True:
        if not COMMAND_QUEUE:
            COMMAND_QUEUE.append("INFO")
            COMMAND_QUEUE.append("GET OPTION")
            COMMAND_QUEUE.append("GET ATTLOG")
            log("🤖 Auto-added initial commands to queue")
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_send_commands())
    log("🚀 eSSL Probe Started")

# ---------------- UI ROUTES ----------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    current_time = datetime.utcnow().isoformat() + "Z"
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS,
            "attendance": ATTENDANCE_DATA,
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": COMMAND_QUEUE,
            "device_sn": DEVICE_SN,
            "current_time": current_time
        }
    )

@app.get("/get_logs")
async def get_logs():
    return {
        "logs": LOGS,
        "attendance": ATTENDANCE_DATA,
        "queue": COMMAND_QUEUE,
        "queue_count": len(COMMAND_QUEUE),
        "attendance_count": len(ATTENDANCE_DATA),
        "logs_count": len(LOGS),
        "device_sn": DEVICE_SN
    }

@app.post("/send_command", response_class=HTMLResponse)
async def send_command(
    request: Request,
    endpoint: str = Form(...),
    command: str = Form(...)
):
    if endpoint == "/iclock/getrequest.aspx":
        COMMAND_QUEUE.append(command)
        log(f"✅ COMMAND QUEUED: {command}")
    else:
        log(f"⚠️  This endpoint ({endpoint}) doesn't support queued commands")
    
    current_time = datetime.utcnow().isoformat() + "Z"
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS,
            "attendance": ATTENDANCE_DATA,
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": COMMAND_QUEUE,
            "device_sn": DEVICE_SN,
            "current_time": current_time
        }
    )

@app.post("/clear_queue", response_class=HTMLResponse)
async def clear_queue(request: Request):
    global COMMAND_QUEUE
    COMMAND_QUEUE = []
    log("🗑️ Command queue cleared")
    current_time = datetime.utcnow().isoformat() + "Z"
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS,
            "attendance": ATTENDANCE_DATA,
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": COMMAND_QUEUE,
            "device_sn": DEVICE_SN,
            "current_time": current_time
        }
    )

@app.post("/clear_logs")
async def clear_logs(request: Request):
    global LOGS, ATTENDANCE_DATA
    LOGS = []
    ATTENDANCE_DATA = []
    log("🧹 All logs cleared")
    return PlainTextResponse("OK")

# ---------------- DEVICE ENDPOINTS ----------------

@app.api_route("/iclock/cdata.aspx", methods=["GET", "POST"])
async def iclock_cdata(request: Request):
    body = (await request.body()).decode(errors="ignore")
    await log_request(request, body)

    if request.method == "GET":
        return PlainTextResponse("OK")

    if request.method == "POST":
        lines = body.splitlines()
        
        for line in lines:
            if not line:
                continue
                
            if line.startswith("ATTLOG"):
                log("📥 ATTENDANCE DATA RECEIVED")
                if "SN=" in line:
                    global DEVICE_SN
                    DEVICE_SN = line.split("SN=")[1].strip()
                    log(f"📱 Device SN: {DEVICE_SN}")
                
            elif "=" in line and not line.startswith("ATTLOG"):
                log_attendance(f"ATT → {line}")
                log(f"👤 {line}")
                
                parts = line.split("\t")
                if len(parts) >= 3:
                    user_id = parts[0]
                    timestamp = parts[1]
                    status = parts[2] if len(parts) > 2 else ""
                    log(f"   👤 User: {user_id}, ⏰ Time: {timestamp}, 📊 Status: {status}")
        
        return PlainTextResponse("OK")

@app.get("/iclock/getrequest.aspx")
async def iclock_getrequest(request: Request):
    log("📡 DEVICE PULLING COMMAND")
    
    await asyncio.sleep(0.5)
    
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 SENDING COMMAND: {command}")
        
        if command == "GET ATTLOG":
            async def add_attlog_later():
                await asyncio.sleep(60)
                if "GET ATTLOG" not in COMMAND_QUEUE:
                    COMMAND_QUEUE.append("GET ATTLOG")
                    log("🔄 Auto-added GET ATTLOG to queue (60s interval)")
            
            asyncio.create_task(add_attlog_later())
        
        return PlainTextResponse(command)
    else:
        log("📤 SENDING DEFAULT: GET ATTLOG")
        return PlainTextResponse("GET ATTLOG")

@app.get("/iclock/registry.aspx")
async def iclock_registry(request: Request):
    log("📝 DEVICE REGISTRATION")
    return PlainTextResponse("OK")

@app.post("/iclock/devicecmd.aspx")
async def iclock_devicecmd(request: Request):
    body = (await request.body()).decode(errors="ignore")
    log(f"📋 DEVICE COMMAND RESPONSE: {body[:200]}")
    return PlainTextResponse("OK")

@app.get("/export_attendance")
async def export_attendance():
    if not ATTENDANCE_DATA:
        return PlainTextResponse("No attendance data available")
    
    content = "\n".join(ATTENDANCE_DATA)
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f"attachment; filename=attendance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Content-Type": "text/plain"
        }
    )

@app.get("/favicon.ico")
async def favicon():
    return PlainTextResponse("")