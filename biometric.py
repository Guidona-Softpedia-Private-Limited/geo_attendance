import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime

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
    "SET OPTION PUSH=1"
]

LOGS = []

def log(msg: str):
    ts = f"{datetime.utcnow().isoformat()}Z - {msg}"
    print(ts)
    LOGS.append(ts)
    if len(LOGS) > 200:
        LOGS.pop(0)

async def log_request(request: Request, body: str):
    log("DEVICE REQUEST")
    log(f"  CLIENT   : {request.client.host}")
    log(f"  ENDPOINT : {request.url.path}")
    log(f"  METHOD   : {request.method}")
    log(f"  QUERY    : {dict(request.query_params)}")
    log(f"  BODY     : {body if body else '<empty>'}")
    log("-" * 60)

# ---------------- UI HOME ----------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS,
            "endpoints": ENDPOINTS,
            "commands": COMMANDS
        }
    )

# ---------------- SEND MANUAL COMMAND ----------------

@app.post("/send_command", response_class=HTMLResponse)
async def send_command(
    request: Request,
    endpoint: str = Form(...),
    command: str = Form(...)
):
    log(f"MANUAL COMMAND → {command} | ENDPOINT → {endpoint}")

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS,
            "endpoints": ENDPOINTS,
            "commands": COMMANDS
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
        if "ATTLOG" in body:
            log("📥 ATTENDANCE RECEIVED")
            for line in body.splitlines():
                if line and not line.startswith("ATTLOG"):
                    log(f"ATT → {line}")

        elif "RTLOG" in body:
            log("⚡ REALTIME LOG RECEIVED")

        return PlainTextResponse("OK")

# ---------------- COMMAND PULL ----------------

@app.get("/iclock/getrequest.aspx")
async def iclock_getrequest(request: Request):
    log("COMMAND PULL FROM DEVICE")
    return PlainTextResponse("SET OPTION RTLOG=1")

# ---------------- OPTIONAL ROUTES ----------------

@app.get("/iclock/registry.aspx")
async def iclock_registry(request: Request):
    log("DEVICE REGISTRATION")
    return PlainTextResponse("OK")

@app.post("/iclock/devicecmd.aspx")
async def iclock_devicecmd(request: Request):
    body = (await request.body()).decode(errors="ignore")
    log(f"DEVICE CMD RESPONSE: {body}")
    return PlainTextResponse("OK")

# ---------------- FAVICON FIX ----------------

@app.get("/favicon.ico")
async def favicon():
    return PlainTextResponse("")