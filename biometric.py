import os
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
import asyncio
import json
import re
from typing import List, Dict, Any, Optional
import csv
import io
from pathlib import Path
import hashlib
import uvicorn

app = FastAPI(title="eSSL Multi-Device Monitor")

# ---------------- DATA STORAGE ----------------

# Store ALL logs from device (persistent across restarts)
LOGS: List[str] = []
# Store ALL attendance records with detailed parsing
ATTENDANCE_DATA: List[Dict[str, Any]] = []
# Raw data storage
RAW_DATA_STORE: List[Dict[str, Any]] = []
# Command queue
COMMAND_QUEUE: List[str] = []
# Multiple devices support
DEVICES: List[Dict[str, Any]] = []

# File for persistent storage
DATA_DIR = "data"
DATA_FILE = f"{DATA_DIR}/attendance_data.json"
LOG_FILE = f"{DATA_DIR}/device_logs.txt"
RAW_DATA_FILE = f"{DATA_DIR}/raw_data.json"
DEVICES_FILE = f"{DATA_DIR}/devices.json"

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# Track device connection
IS_FETCHING_ALL_LOGS = False
DEVICE_CONNECTED = False
LAST_DEVICE_CONTACT = None

# ---------------- PERSISTENT STORAGE FUNCTIONS ----------------

def load_persistent_data():
    """Load previously saved data from files"""
    global ATTENDANCE_DATA, LOGS, RAW_DATA_STORE, DEVICES
    
    try:
        # Load attendance data
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                ATTENDANCE_DATA = data.get('attendance', [])
                print(f"📂 Loaded {len(ATTENDANCE_DATA)} attendance records from file")
    except Exception as e:
        print(f"⚠️ Error loading persistent data: {e}")
    
    try:
        # Load logs
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                LOGS = [line.strip() for line in f.readlines() if line.strip()]
                print(f"📂 Loaded {len(LOGS)} log entries from file")
    except Exception as e:
        print(f"⚠️ Error loading logs: {e}")
    
    try:
        # Load raw data
        if os.path.exists(RAW_DATA_FILE):
            with open(RAW_DATA_FILE, 'r', encoding='utf-8') as f:
                RAW_DATA_STORE = json.load(f)
                print(f"📂 Loaded {len(RAW_DATA_STORE)} raw data entries")
    except Exception as e:
        print(f"⚠️ Error loading raw data: {e}")
    
    try:
        # Load devices
        if os.path.exists(DEVICES_FILE):
            with open(DEVICES_FILE, 'r', encoding='utf-8') as f:
                DEVICES = json.load(f)
                print(f"📂 Loaded {len(DEVICES)} devices")
    except Exception as e:
        print(f"⚠️ Error loading devices: {e}")

def save_persistent_data():
    """Save current data to files"""
    try:
        # Save attendance data
        data = {
            'attendance': ATTENDANCE_DATA,
            'last_updated': datetime.utcnow().isoformat()
        }
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving persistent data: {e}")
    
    try:
        # Save logs (keep last 2000 lines to avoid file getting too large)
        logs_to_save = LOGS[-2000:] if len(LOGS) > 2000 else LOGS
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            for log_entry in logs_to_save:
                f.write(log_entry + "\n")
    except Exception as e:
        print(f"⚠️ Error saving logs: {e}")
    
    try:
        # Save raw data
        with open(RAW_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(RAW_DATA_STORE[-1000:], f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving raw data: {e}")
    
    try:
        # Save devices
        with open(DEVICES_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEVICES, f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving devices: {e}")

def log(msg: str):
    """Add a log entry with timestamp"""
    ts = f"{datetime.utcnow().isoformat()}Z - {msg}"
    print(ts)
    LOGS.append(ts)
    # Save logs periodically
    if len(LOGS) % 10 == 0:
        save_persistent_data()

def store_raw_data(device_sn: str, raw_data: str, direction: str = "incoming"):
    """Store raw data for display"""
    data_hash = hashlib.md5(raw_data.encode()).hexdigest()
    
    raw_entry = {
        'id': data_hash,
        'timestamp': datetime.utcnow().isoformat(),
        'device_sn': device_sn,
        'raw_data': raw_data,
        'length': len(raw_data),
        'direction': direction,
        'hex_preview': ' '.join([f"{ord(c):02x}" for c in raw_data[:64]]) + ("..." if len(raw_data) > 64 else ""),
        'ascii_preview': ''.join([c if 32 <= ord(c) < 127 else '.' for c in raw_data[:64]]) + ("..." if len(raw_data) > 64 else "")
    }
    
    RAW_DATA_STORE.append(raw_entry)
    
    # Keep only last 1000 entries
    if len(RAW_DATA_STORE) > 1000:
        RAW_DATA_STORE.pop(0)
    
    # Save immediately
    save_persistent_data()
    
    return data_hash

def update_device_info(sn: str, ip_address: str = "", data: Dict[str, Any] = None):
    """Update or create device information"""
    global DEVICES
    
    # Clean SN (remove spaces, special characters)
    sn = str(sn).strip()
    
    # Find existing device
    device_index = -1
    for i, device in enumerate(DEVICES):
        if device.get('sn') == sn:
            device_index = i
            break
    
    now = datetime.utcnow()
    
    if device_index >= 0:
        # Update existing device
        DEVICES[device_index]['last_seen'] = now.isoformat()
        DEVICES[device_index]['last_seen_seconds'] = 0
        DEVICES[device_index]['comms_count'] = DEVICES[device_index].get('comms_count', 0) + 1
        
        if ip_address:
            DEVICES[device_index]['ip_address'] = ip_address
        
        if data:
            DEVICES[device_index].update(data)
    else:
        # Create new device with full serial number
        device_data = {
            'sn': sn,  # Full serial number
            'ip_address': ip_address or 'Unknown',
            'device_name': f"Device {sn}" if len(sn) <= 12 else f"Device {sn[-12:]}",
            'short_sn': sn[-8:] if len(sn) > 8 else sn,
            'first_seen': now.isoformat(),
            'last_seen': now.isoformat(),
            'last_seen_seconds': 0,
            'records_count': 0,
            'comms_count': 1,
            'status': 'online',
            'params': {}
        }
        
        if data:
            device_data.update(data)
        
        DEVICES.append(device_data)
        log(f"📱 New device detected: {sn}")
    
    # Update last seen seconds for all devices
    for device in DEVICES:
        try:
            last_seen_str = device['last_seen'].replace('Z', '+00:00')
            last_seen = datetime.fromisoformat(last_seen_str)
            device['last_seen_seconds'] = (now - last_seen).total_seconds()
        except:
            device['last_seen_seconds'] = 999999
    
    save_persistent_data()
    return device_index >= 0

def parse_attendance_line(line: str, device_sn: str = "Unknown") -> Dict[str, Any]:
    """
    Parse attendance line in format:
    USER_ID\tTIMESTAMP\tSTATUS\tVERIFICATION\tWORKCODE
    """
    parts = line.split('\t')
    if len(parts) < 3:
        return {}
    
    record = {
        'user_id': parts[0],
        'timestamp': parts[1],
        'status': parts[2],
        'verification': parts[3] if len(parts) > 3 else '',
        'workcode': parts[4] if len(parts) > 4 else '',
        'device_sn': device_sn,  # Store full serial number
        'received_at': datetime.utcnow().isoformat(),
        'raw': line
    }
    
    # Find device name
    device_name = "Unknown Device"
    for device in DEVICES:
        if device.get('sn') == device_sn:
            device_name = device.get('device_name', f"Device {device_sn}")
            break
    
    record['device_name'] = device_name
    
    # Map status codes to human readable
    status_map = {
        '0': 'Check-in',
        '1': 'Check-out',
        '2': 'Break-out',
        '3': 'Break-in',
        '4': 'Overtime-in',
        '5': 'Overtime-out',
        '255': 'Error'
    }
    record['status_text'] = status_map.get(record['status'], 'Unknown')
    
    # Parse date and time for display
    try:
        dt = datetime.strptime(record['timestamp'], "%Y-%m-%d %H:%M:%S")
        record['display_date'] = dt.strftime("%Y-%m-%d")
        record['display_time'] = dt.strftime("%H:%M:%S")
        record['datetime_obj'] = dt.isoformat()
    except:
        record['display_date'] = record['timestamp'].split()[0] if ' ' in record['timestamp'] else record['timestamp']
        record['display_time'] = record['timestamp'].split()[1] if ' ' in record['timestamp'] else ''
        record['datetime_obj'] = None
    
    # Generate hash for raw data reference
    record['raw_data_hash'] = hashlib.md5(line.encode()).hexdigest()
    
    return record

async def log_request(request: Request, body: str):
    """Log device request details"""
    global DEVICE_CONNECTED, LAST_DEVICE_CONTACT
    DEVICE_CONNECTED = True
    LAST_DEVICE_CONTACT = datetime.utcnow()
    
    log("DEVICE REQUEST")
    log(f"  CLIENT   : {request.client.host if request.client else 'Unknown'}")
    log(f"  ENDPOINT : {request.url.path}")
    log(f"  METHOD   : {request.method}")
    log(f"  QUERY    : {dict(request.query_params)}")
    if body and len(body) > 1000:
        log(f"  BODY     : {body[:1000]}... ({len(body)} chars)")
    else:
        log(f"  BODY     : {body if body else '<empty>'}")
    log("-" * 60)

async def auto_send_commands():
    """Automatically send commands to device periodically"""
    global IS_FETCHING_ALL_LOGS, LAST_DEVICE_CONTACT
    
    first_run = True
    
    while True:
        try:
            # Check if device was recently connected
            device_active = LAST_DEVICE_CONTACT and (datetime.utcnow() - LAST_DEVICE_CONTACT).total_seconds() < 300
            
            if first_run and device_active:
                # Initial sequence
                COMMAND_QUEUE.append("INFO")
                COMMAND_QUEUE.append("GET OPTION")
                COMMAND_QUEUE.append("SET OPTION RTLOG=1")
                COMMAND_QUEUE.append("SET OPTION PUSH=1")
                COMMAND_QUEUE.append("GET ATTLOG ALL")
                log("🤖 Auto-added initial commands")
                IS_FETCHING_ALL_LOGS = True
                first_run = False
            
            await asyncio.sleep(10)
            
            # If device is active but queue is empty, add attendance command
            if device_active and not COMMAND_QUEUE:
                COMMAND_QUEUE.append("GET ATTLOG")
                log("🔄 Added GET ATTLOG to empty queue")
                
        except Exception as e:
            log(f"⚠️ Error in auto_send_commands: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    """Initialize application"""
    load_persistent_data()
    asyncio.create_task(auto_send_commands())
    asyncio.create_task(periodic_save())
    asyncio.create_task(check_device_status())
    log("🚀 eSSL Multi-Device Monitor Started")

async def periodic_save():
    """Periodically save data to disk"""
    while True:
        await asyncio.sleep(60)
        save_persistent_data()

async def check_device_status():
    """Check if device is still connected"""
    global DEVICE_CONNECTED, LAST_DEVICE_CONTACT
    while True:
        await asyncio.sleep(30)
        if LAST_DEVICE_CONTACT and (datetime.utcnow() - LAST_DEVICE_CONTACT).total_seconds() > 120:
            if DEVICE_CONNECTED:
                DEVICE_CONNECTED = False
                log("⚠️ Device connection lost - no contact for 2 minutes")

# ---------------- UI ROUTES ----------------

templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main dashboard page"""
    current_time = datetime.utcnow()
    
    # Get statistics
    today = current_time.date()
    today_records = [r for r in ATTENDANCE_DATA 
                    if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))]
    
    # Calculate device statistics
    online_devices = sum(1 for d in DEVICES if d.get('last_seen_seconds', 0) < 300)
    
    # Calculate total data size
    total_data_size = sum(len(r.get('raw', '')) for r in ATTENDANCE_DATA)
    total_data_bytes = sum(len(r.get('raw', '')) for r in ATTENDANCE_DATA)
    total_comms = sum(d.get('comms_count', 0) for d in DEVICES)
    
    # Format device data for display
    display_devices = []
    for device in DEVICES:
        display_device = device.copy()
        
        # Format dates
        try:
            first_seen_str = device['first_seen'].replace('Z', '+00:00')
            last_seen_str = device['last_seen'].replace('Z', '+00:00')
            first_seen_dt = datetime.fromisoformat(first_seen_str)
            last_seen_dt = datetime.fromisoformat(last_seen_str)
            display_device['first_seen'] = first_seen_dt.strftime("%Y-%m-%d %H:%M")
            display_device['last_seen'] = last_seen_dt.strftime("%Y-%m-%d %H:%M")
        except:
            display_device['first_seen'] = device.get('first_seen', 'Unknown')
            display_device['last_seen'] = device.get('last_seen', 'Unknown')
        
        display_devices.append(display_device)
    
    # Get recent attendance for display
    recent_attendance = []
    for record in ATTENDANCE_DATA[-50:]:
        display_record = record.copy()
        
        # Ensure device info is complete
        if not display_record.get('device_sn'):
            display_record['device_sn'] = 'Unknown'
        
        # Get device name if not already present
        if not display_record.get('device_name') or display_record['device_name'] == 'Unknown Device':
            for device in DEVICES:
                if device.get('sn') == display_record['device_sn']:
                    display_record['device_name'] = device.get('device_name', f"Device {display_record['device_sn']}")
                    break
        
        recent_attendance.append(display_record)
    
    # Get recent raw data
    recent_raw_data = RAW_DATA_STORE[-20:]
    
    # Get server URL for display
    server_url = str(request.base_url).rstrip('/')
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "devices": display_devices,
            "total_records": len(ATTENDANCE_DATA),
            "live_records": len(today_records),
            "total_data_size": f"{total_data_size / 1024:.1f} KB",
            "total_data_bytes": total_data_bytes,
            "total_comms": total_comms,
            "online_devices": online_devices,
            "last_update_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "recent_attendance": recent_attendance,
            "recent_raw_data": recent_raw_data,
            "logs": LOGS[-100:],
            "server_url": server_url,
            "now": current_time
        }
    )

# ---------------- API ENDPOINTS ----------------

@app.get("/api/devices")
async def get_devices():
    """Get all devices"""
    return {"devices": DEVICES, "count": len(DEVICES)}

@app.post("/api/devices/rescan")
async def rescan_devices():
    """Rescan for devices"""
    log("🔄 Device rescan initiated")
    return {"message": "Device rescan initiated", "devices_count": len(DEVICES)}

@app.get("/api/device/{device_sn}")
async def get_device(device_sn: str):
    """Get specific device details"""
    for device in DEVICES:
        if device.get('sn') == device_sn:
            return device
    return JSONResponse({"error": "Device not found"}, status_code=404)

@app.post("/api/device/{device_sn}/command")
async def send_device_command(device_sn: str, command: str = Form(...)):
    """Send command to specific device"""
    COMMAND_QUEUE.append(command)
    log(f"📤 Command queued for {device_sn}: {command}")
    return {"status": "success", "command": command, "device_sn": device_sn}

@app.post("/api/command/broadcast")
async def broadcast_command(command: str = Form(...)):
    """Send command to all devices"""
    COMMAND_QUEUE.append(command)
    log(f"📢 Command broadcasted: {command}")
    return {"status": "success", "command": command, "devices_count": len(DEVICES)}

@app.post("/api/command/send")
async def send_command(command: str = Form(...)):
    """Send manual command"""
    COMMAND_QUEUE.append(command)
    log(f"📤 Manual command queued: {command}")
    return {"status": "success", "command": command}

@app.get("/api/raw-data/recent")
async def get_recent_raw_data(limit: int = 30):
    """Get recent raw data"""
    recent_data = RAW_DATA_STORE[-limit:]
    return recent_data

@app.get("/api/device/{device_sn}/raw-data")
async def get_device_raw_data(device_sn: str):
    """Get raw data for specific device"""
    device_data = [rd for rd in RAW_DATA_STORE if rd.get('device_sn') == device_sn]
    if not device_data:
        return JSONResponse({"error": "No raw data found for device"}, status_code=404)
    
    total_bytes = sum(rd.get('length', 0) for rd in device_data)
    return {
        "raw_data": '\n'.join([rd.get('raw_data', '') for rd in device_data[-50:]]),
        "count": len(device_data),
        "total_bytes": total_bytes
    }

@app.get("/api/raw-data/{data_hash}")
async def get_raw_data(data_hash: str):
    """Get specific raw data by hash"""
    for rd in RAW_DATA_STORE:
        if rd.get('id') == data_hash:
            return rd
    return JSONResponse({"error": "Raw data not found"}, status_code=404)

@app.get("/api/attendance/recent")
async def get_recent_attendance(limit: int = 100):
    """Get recent attendance records"""
    recent = ATTENDANCE_DATA[-limit:]
    
    # Ensure device info is complete
    for record in recent:
        if not record.get('device_sn'):
            record['device_sn'] = 'Unknown'
        
        # Find device name
        device_name = "Unknown Device"
        for device in DEVICES:
            if device.get('sn') == record['device_sn']:
                device_name = device.get('device_name', f"Device {record['device_sn']}")
                break
        
        record['device_name'] = device_name
        
        # Ensure hash exists
        if not record.get('raw_data_hash'):
            record['raw_data_hash'] = hashlib.md5(record.get('raw', '').encode()).hexdigest()
    
    return recent

@app.get("/api/export/csv")
async def export_csv():
    """Export attendance as CSV"""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(["User ID", "Date", "Time", "Status", "Status Text", "Verification", "Workcode", "Device SN", "Device Name", "Received At"])
    
    # Write data
    for record in ATTENDANCE_DATA:
        writer.writerow([
            record.get('user_id', ''),
            record.get('display_date', ''),
            record.get('display_time', ''),
            record.get('status', ''),
            record.get('status_text', ''),
            record.get('verification', ''),
            record.get('workcode', ''),
            record.get('device_sn', 'Unknown'),
            record.get('device_name', 'Unknown Device'),
            record.get('received_at', '')
        ])
    
    content = output.getvalue()
    filename = f"attendance_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "text/csv"
        }
    )

@app.post("/api/fetch/all")
async def fetch_all_devices():
    """Fetch all data from all devices"""
    global IS_FETCHING_ALL_LOGS
    
    COMMAND_QUEUE.append("GET ATTLOG ALL")
    IS_FETCHING_ALL_LOGS = True
    log("🚀 Fetch ALL attendance logs initiated")
    
    return {"status": "started", "devices_count": len(DEVICES), "message": "Fetching ALL attendance logs from all devices"}

@app.get("/api/logs/recent")
async def get_recent_logs(limit: int = 150):
    """Get recent logs"""
    return LOGS[-limit:]

@app.post("/api/logs/export")
async def export_logs():
    """Export logs"""
    content = '\n'.join(LOGS[-1000:])
    filename = f"essl_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "text/plain"
        }
    )

@app.delete("/api/logs/clear")
async def clear_logs():
    """Clear logs"""
    global LOGS
    LOGS = []
    log("🧹 All logs cleared")
    save_persistent_data()
    return {"message": "Logs cleared"}

# ---------------- DEVICE ENDPOINTS ----------------

@app.api_route("/iclock/cdata.aspx", methods=["GET", "POST"])
async def iclock_cdata(request: Request):
    """Handle ALL device data - this is the MAIN endpoint"""
    try:
        body = (await request.body()).decode('utf-8', errors='ignore')
    except:
        body = ""
    
    # Get device SN from query params
    device_sn = request.query_params.get("SN", "")
    
    # If SN is empty in query, try to extract from body
    if not device_sn and body:
        # Look for SN= in body (case insensitive)
        sn_match = re.search(r'SN=([^\s\r\n]+)', body, re.IGNORECASE)
        if sn_match:
            device_sn = sn_match.group(1).strip()
    
    # If still no SN, try to extract from headers or IP
    if not device_sn:
        # Try to get from User-Agent or other headers
        user_agent = request.headers.get("User-Agent", "")
        if "SN=" in user_agent.upper():
            sn_match = re.search(r'SN=([^\s]+)', user_agent, re.IGNORECASE)
            if sn_match:
                device_sn = sn_match.group(1).strip()
    
    # Fallback to IP-based device ID
    if not device_sn:
        client_ip = request.client.host if request.client else "Unknown"
        device_sn = f"IP-{client_ip.replace('.', '-')}"
    
    # Clean the device SN
    device_sn = device_sn.strip()
    
    # Store raw data
    data_hash = store_raw_data(device_sn, body, "incoming")
    await log_request(request, body)
    
    # Update device info
    client_ip = request.client.host if request.client else "Unknown"
    update_device_info(device_sn, client_ip, {
        "last_request": datetime.utcnow().isoformat(),
        "endpoint": "/iclock/cdata.aspx",
        "method": request.method
    })

    if request.method == "GET":
        # Device is checking if server is alive
        response = "OK=0"
        store_raw_data(device_sn, response, "outgoing")
        return PlainTextResponse(response)

    if request.method == "POST":
        lines = body.splitlines()
        attendance_count = 0
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Try to parse as attendance (tab-separated)
            if '\t' in line:
                parts = line.split('\t')
                
                # Check if this looks like attendance data
                if len(parts) >= 2:
                    record = parse_attendance_line(line, device_sn)
                    if record:
                        # Check for duplicate
                        record_key = f"{record['user_id']}_{record['timestamp']}_{record['status']}_{device_sn}"
                        existing = any(
                            f"{r.get('user_id')}_{r.get('timestamp')}_{r.get('status')}_{r.get('device_sn')}" == record_key 
                            for r in ATTENDANCE_DATA
                        )
                        
                        if not existing:
                            ATTENDANCE_DATA.append(record)
                            attendance_count += 1
                            
                            # Update device record count
                            for device in DEVICES:
                                if device.get('sn') == device_sn:
                                    device['records_count'] = device.get('records_count', 0) + 1
                                    break
            elif '=' in line:
                # Parse parameters (e.g., SN=OCK194560212)
                key_value = line.split('=', 1)
                if len(key_value) == 2:
                    key, value = key_value
                    if key.upper() == 'SN' and value and value != device_sn:
                        # Update device SN if found in body
                        device_sn = value.strip()
                        update_device_info(device_sn, client_ip)
        
        if attendance_count > 0:
            save_persistent_data()
            log(f"🎉 Added {attendance_count} attendance records from {device_sn} (Total: {len(ATTENDANCE_DATA)})")
        
        response = "OK"
        store_raw_data(device_sn, response, "outgoing")
        return PlainTextResponse(response)

@app.get("/iclock/getrequest.aspx")
async def iclock_getrequest(request: Request):
    """Device pulls commands from here"""
    
    # Get device SN from query params
    device_sn = request.query_params.get("SN", "")
    if not device_sn:
        return PlainTextResponse("ERROR: SN parameter required")
    
    update_device_info(device_sn, request.client.host if request.client else "Unknown", {
        "last_pull": datetime.utcnow().isoformat()
    })
    
    # Log the pull request
    log(f"📡 Device pulling command (SN: {device_sn})")
    
    # Send next command if available
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 SENDING to {device_sn}: {command}")
        
        # Store command in raw data
        store_raw_data(device_sn, command, "outgoing")
        
        return PlainTextResponse(command)
    else:
        # Default response - check for data
        default_cmd = "GET ATTLOG"
        log(f"📤 SENDING default to {device_sn}: {default_cmd}")
        store_raw_data(device_sn, default_cmd, "outgoing")
        return PlainTextResponse(default_cmd)

@app.get("/iclock/registry.aspx")
async def iclock_registry(request: Request):
    """Device registration endpoint"""
    log("📝 DEVICE REGISTRATION")
    
    device_sn = ""
    device_data = {}
    
    for key, value in request.query_params.items():
        if key.upper() == "SN":
            device_sn = value
            log(f"📱 Registered Device SN: {device_sn}")
        device_data[key] = value
    
    log(f"📋 Registration params: {dict(request.query_params)}")
    
    # Update device info
    if device_sn:
        update_device_info(device_sn, request.client.host if request.client else "Unknown", {
            "registered": True,
            "registration_time": datetime.utcnow().isoformat(),
            **device_data
        })
    
    save_persistent_data()
    return PlainTextResponse("OK")

@app.post("/iclock/devicecmd.aspx")
async def iclock_devicecmd(request: Request):
    """Device command responses"""
    try:
        body = (await request.body()).decode('utf-8', errors='ignore')
    except:
        body = ""
    
    # Extract device SN from URL or body
    device_sn = request.query_params.get("SN", "Unknown")
    
    # Try to extract SN from body if not in URL
    if device_sn == "Unknown" and body:
        sn_match = re.search(r'SN=([^\s\r\n]+)', body, re.IGNORECASE)
        if sn_match:
            device_sn = sn_match.group(1).strip()
    
    # Store raw data
    store_raw_data(device_sn, body, "incoming")
    
    log(f"📋 DEVICE CMD RESPONSE from {device_sn}: {body[:200]}...")
    
    # Parse INFO responses to update device params
    device_params = {}
    if "=" in body:
        lines = body.splitlines()
        for line in lines:
            if '=' in line:
                try:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    device_params[key] = value
                except:
                    pass
    
    # Update device info with parameters
    update_device_info(device_sn, request.client.host if request.client else "Unknown", {
        "last_command_response": datetime.utcnow().isoformat(),
        "command_response": body[:500],
        "params": device_params
    })
    
    save_persistent_data()
    return PlainTextResponse("OK")

# ---------------- UTILITY ENDPOINTS ----------------

@app.get("/favicon.ico")
async def favicon():
    return PlainTextResponse("")

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "devices": len(DEVICES),
        "attendance_records": len(ATTENDANCE_DATA),
        "raw_data_entries": len(RAW_DATA_STORE)
    }
