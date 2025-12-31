import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime
import asyncio
import json

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
ATTENDANCE_DATA = []  # Store attendance separately
COMMAND_QUEUE = []  # Queue for commands to send to device
DEVICE_SN = "Unknown"  # Store device serial number

def log(msg: str):
    ts = f"{datetime.utcnow().isoformat()}Z - {msg}"
    print(ts)
    LOGS.append(ts)
    if len(LOGS) > 500:  # Increased buffer
        LOGS.pop(0)

def log_attendance(msg: str):
    ts = f"{datetime.utcnow().isoformat()}Z - {msg}"
    ATTENDANCE_DATA.append(ts)
    if len(ATTENDANCE_DATA) > 1000:  # Increased buffer
        ATTENDANCE_DATA.pop(0)

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

# ---------------- AUTO COMMAND SENDER ----------------

async def auto_send_commands():
    """Automatically send commands to device when it connects"""
    while True:
        # If command queue is empty, add GET ATTLOG to get old data
        if not COMMAND_QUEUE:
            COMMAND_QUEUE.append("INFO")
            COMMAND_QUEUE.append("GET OPTION")
            COMMAND_QUEUE.append("GET ATTLOG")
            log("🤖 Auto-added initial commands to queue")
        
        await asyncio.sleep(30)  # Check every 30 seconds instead of 5

@app.on_event("startup")
async def startup_event():
    # Start auto command sender in background
    asyncio.create_task(auto_send_commands())
    log("🚀 eSSL Probe Started")

# ---------------- UI HOME ----------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS,
            "attendance": ATTENDANCE_DATA[-100:],  # Show last 100 attendance
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": COMMAND_QUEUE,
            "device_sn": DEVICE_SN
        }
    )

# ---------------- GET LOGS DATA (for AJAX) ----------------

@app.get("/get_logs")
async def get_logs():
    """Return logs data for AJAX updates"""
    return {
        "logs": LOGS[-50:],  # Last 50 logs
        "attendance": ATTENDANCE_DATA[-30:],  # Last 30 attendance
        "queue": COMMAND_QUEUE,
        "queue_count": len(COMMAND_QUEUE),
        "attendance_count": len(ATTENDANCE_DATA),
        "logs_count": len(LOGS),
        "device_sn": DEVICE_SN
    }

# ---------------- SEND MANUAL COMMAND ----------------

@app.post("/send_command", response_class=HTMLResponse)
async def send_command(
    request: Request,
    endpoint: str = Form(...),
    command: str = Form(...)
):
    if endpoint == "/iclock/getrequest.aspx":
        # Add to command queue for device
        COMMAND_QUEUE.append(command)
        log(f"✅ COMMAND QUEUED: {command}")
    else:
        log(f"⚠️  This endpoint ({endpoint}) doesn't support queued commands")
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS,
            "attendance": ATTENDANCE_DATA[-100:],
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": COMMAND_QUEUE,
            "device_sn": DEVICE_SN
        }
    )

# ---------------- CLEAR COMMAND QUEUE ----------------

@app.post("/clear_queue", response_class=HTMLResponse)
async def clear_queue(request: Request):
    global COMMAND_QUEUE
    COMMAND_QUEUE = []
    log("🗑️ Command queue cleared")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS,
            "attendance": ATTENDANCE_DATA[-100:],
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": COMMAND_QUEUE,
            "device_sn": DEVICE_SN
        }
    )

# ---------------- iCLOCK CDATA ----------------

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
                # Extract SN from ATTLOG line
                if "SN=" in line:
                    global DEVICE_SN
                    DEVICE_SN = line.split("SN=")[1].strip()
                    log(f"📱 Device SN: {DEVICE_SN}")
                
            elif "=" in line and not line.startswith("ATTLOG"):
                # This is attendance data
                log_attendance(f"ATT → {line}")
                log(f"👤 {line}")
                
                # Parse attendance data
                parts = line.split("\t")
                if len(parts) >= 3:
                    user_id = parts[0]
                    timestamp = parts[1]
                    status = parts[2] if len(parts) > 2 else ""
                    log(f"   👤 User: {user_id}, ⏰ Time: {timestamp}, 📊 Status: {status}")
        
        return PlainTextResponse("OK")

# ---------------- COMMAND PULL ----------------

@app.get("/iclock/getrequest.aspx")
async def iclock_getrequest(request: Request):
    log("📡 DEVICE PULLING COMMAND")
    
    # Wait a bit to ensure device is ready
    await asyncio.sleep(0.5)
    
    # If queue has commands, send next one
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 SENDING COMMAND: {command}")
        
        # If we sent GET ATTLOG, add it back after 60 seconds to keep getting data
        if command == "GET ATTLOG":
            # Schedule to add GET ATTLOG again after 60 seconds
            async def add_attlog_later():
                await asyncio.sleep(60)
                if "GET ATTLOG" not in COMMAND_QUEUE:
                    COMMAND_QUEUE.append("GET ATTLOG")
                    log("🔄 Auto-added GET ATTLOG to queue (60s interval)")
            
            asyncio.create_task(add_attlog_later())
        
        return PlainTextResponse(command)
    else:
        # Default: ask for attendance data
        log("📤 SENDING DEFAULT: GET ATTLOG")
        return PlainTextResponse("GET ATTLOG")

# ---------------- OPTIONAL ROUTES ----------------

@app.get("/iclock/registry.aspx")
async def iclock_registry(request: Request):
    log("📝 DEVICE REGISTRATION")
    return PlainTextResponse("OK")

@app.post("/iclock/devicecmd.aspx")
async def iclock_devicecmd(request: Request):
    body = (await request.body()).decode(errors="ignore")
    log(f"📋 DEVICE COMMAND RESPONSE: {body[:200]}")
    return PlainTextResponse("OK")

# ---------------- EXPORT ATTENDANCE ----------------

@app.get("/export_attendance")
async def export_attendance():
    """Export all attendance data as text file"""
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

# ---------------- CLEAR LOGS ----------------

@app.post("/clear_logs")
async def clear_logs(request: Request):
    global LOGS, ATTENDANCE_DATA
    LOGS = []
    ATTENDANCE_DATA = []
    log("🧹 All logs cleared")
    return PlainTextResponse("OK")

# ---------------- FAVICON FIX ----------------

@app.get("/favicon.ico")
async def favicon():
    return PlainTextResponse("")