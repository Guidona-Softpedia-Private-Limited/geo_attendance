import os
import json
import asyncio
import hashlib
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from collections import defaultdict, OrderedDict
import urllib.parse
from pathlib import Path
import ipaddress

app = FastAPI(title="eSSL Multi-Device Monitor")

# ------------- CONFIGURATION -------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

ATTENDANCE_FILE = DATA_DIR / "attendance_data.json"
DEVICES_FILE = DATA_DIR / "devices.json"
RAW_DATA_FILE = DATA_DIR / "raw_data.json"
SYSTEM_LOGS_FILE = DATA_DIR / "system_logs.json"

# ------------- DATA STORAGE -------------
DEVICES: Dict[str, Dict[str, Any]] = {}  # device_sn -> device_info
ATTENDANCE_RECORDS: List[Dict[str, Any]] = []
RAW_DATA_STORE: List[Dict[str, Any]] = []  # Store all raw communications
SYSTEM_LOGS: List[str] = []
COMMAND_QUEUE: Dict[str, List[str]] = defaultdict(list)  # device_sn -> commands
TOTAL_DATA_BYTES: int = 0
TOTAL_COMMUNICATIONS: int = 0

# ------------- PERSISTENCE FUNCTIONS -------------
def load_all_data():
    """Load all data from files"""
    global DEVICES, ATTENDANCE_RECORDS, RAW_DATA_STORE, SYSTEM_LOGS, TOTAL_DATA_BYTES, TOTAL_COMMUNICATIONS
    
    try:
        # Load attendance data
        if ATTENDANCE_FILE.exists():
            with open(ATTENDANCE_FILE, 'r') as f:
                data = json.load(f)
                ATTENDANCE_RECORDS = data.get('records', [])
                TOTAL_DATA_BYTES = data.get('total_bytes', 0)
                TOTAL_COMMUNICATIONS = data.get('total_comms', 0)
                print(f"📂 Loaded {len(ATTENDANCE_RECORDS)} attendance records")
        
        # Load devices
        if DEVICES_FILE.exists():
            with open(DEVICES_FILE, 'r') as f:
                devices_data = json.load(f)
                DEVICES = devices_data.get('devices', {})
                print(f"📱 Loaded {len(DEVICES)} devices")
        
        # Load raw data (last 1000 entries only)
        if RAW_DATA_FILE.exists():
            with open(RAW_DATA_FILE, 'r') as f:
                raw_data = json.load(f)
                RAW_DATA_STORE = raw_data.get('communications', [])[-1000:]
                print(f"📊 Loaded {len(RAW_DATA_STORE)} raw data entries")
        
        # Load system logs (last 1000 entries)
        if SYSTEM_LOGS_FILE.exists():
            with open(SYSTEM_LOGS_FILE, 'r') as f:
                logs_data = json.load(f)
                SYSTEM_LOGS = logs_data.get('logs', [])[-1000:]
        
    except Exception as e:
        print(f"⚠️ Error loading data: {e}")

def save_all_data():
    """Save all data to files"""
    try:
        # Save attendance data
        attendance_data = {
            'records': ATTENDANCE_RECORDS[-50000:],  # Keep last 50k records
            'total_bytes': TOTAL_DATA_BYTES,
            'total_comms': TOTAL_COMMUNICATIONS,
            'last_updated': datetime.now().isoformat()
        }
        with open(ATTENDANCE_FILE, 'w') as f:
            json.dump(attendance_data, f, indent=2)
        
        # Save devices
        devices_data = {
            'devices': DEVICES,
            'count': len(DEVICES),
            'last_updated': datetime.now().isoformat()
        }
        with open(DEVICES_FILE, 'w') as f:
            json.dump(devices_data, f, indent=2)
        
        # Save raw data (keep last 2000 entries)
        raw_data = {
            'communications': RAW_DATA_STORE[-2000:],
            'total_count': len(RAW_DATA_STORE),
            'total_bytes': TOTAL_DATA_BYTES,
            'last_updated': datetime.now().isoformat()
        }
        with open(RAW_DATA_FILE, 'w') as f:
            json.dump(raw_data, f, indent=2)
        
        # Save system logs (keep last 2000 entries)
        logs_data = {
            'logs': SYSTEM_LOGS[-2000:],
            'total_count': len(SYSTEM_LOGS),
            'last_updated': datetime.now().isoformat()
        }
        with open(SYSTEM_LOGS_FILE, 'w') as f:
            json.dump(logs_data, f, indent=2)
        
        print(f"💾 Data saved: {len(DEVICES)} devices, {len(ATTENDANCE_RECORDS)} records")
        
    except Exception as e:
        print(f"⚠️ Error saving data: {e}")

# ------------- DEVICE MANAGEMENT -------------
def get_or_create_device(device_sn: str, client_ip: str = "") -> Dict[str, Any]:
    """Get existing device or create new one"""
    if device_sn not in DEVICES:
        # Check if device name contains "BOCK194560212" to format it properly
        device_name = f"Device-{device_sn[-6:]}"
        
        # Special handling for device with specific SN pattern
        if "560212" in device_sn and "BOCK" in device_sn.upper():
            # Extract the actual device name if available
            if len(device_sn) > 6:
                # Try to parse device name from the SN
                parts = device_sn.split('_')
                if len(parts) > 1:
                    device_name = parts[0]
                else:
                    device_name = device_sn
            
        DEVICES[device_sn] = {
            'sn': device_sn,
            'first_seen': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'last_seen': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'last_seen_seconds': 0,
            'ip_address': client_ip,
            'device_name': device_name,
            'records_count': 0,
            'comms_count': 0,
            'params': {},
            'status': 'new'
        }
        log(f"🆕 New device detected: {device_sn} ({device_name}) from {client_ip}")
    
    return DEVICES[device_sn]

def update_device_last_seen(device_sn: str, client_ip: str = ""):
    """Update device last seen timestamp"""
    if device_sn in DEVICES:
        device = DEVICES[device_sn]
        device['last_seen'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        device['last_seen_seconds'] = 0  # Will be calculated on frontend
        
        if client_ip and client_ip != 'unknown':
            device['ip_address'] = client_ip
        
        # Update device status
        device['status'] = 'online'

def get_client_ip(request: Request) -> str:
    """Get client IP address"""
    try:
        # Try to get X-Forwarded-For header first (for proxies)
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        
        # Fall back to client host
        return request.client.host if request.client else "unknown"
    except:
        return "unknown"

# ------------- LOGGING -------------
def log(message: str, level: str = "INFO"):
    """Add timestamped log entry"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{level}] {message}"
    SYSTEM_LOGS.append(log_entry)
    print(log_entry)
    
    # Keep logs manageable
    if len(SYSTEM_LOGS) > 2000:
        SYSTEM_LOGS.pop(0)

# ------------- RAW DATA STORAGE -------------
def store_raw_data(device_sn: str, raw_body: str, direction: str = "INCOMING", 
                   source_ip: str = "", parsed_data: Dict = None):
    """Store raw communication data"""
    global TOTAL_DATA_BYTES, TOTAL_COMMUNICATIONS
    
    timestamp = datetime.now().isoformat()
    data_hash = hashlib.md5(f"{timestamp}{raw_body}{device_sn}".encode()).hexdigest()[:16]
    
    raw_entry = {
        'id': data_hash,
        'timestamp': timestamp,
        'device_sn': device_sn,
        'direction': direction,
        'raw_data': raw_body,
        'length': len(raw_body),
        'source_ip': source_ip,
        'parsed_data': parsed_data or {},
        'hash': data_hash
    }
    
    RAW_DATA_STORE.append(raw_entry)
    TOTAL_DATA_BYTES += len(raw_body)
    TOTAL_COMMUNICATIONS += 1
    
    # Update device stats
    if device_sn in DEVICES:
        DEVICES[device_sn]['comms_count'] += 1
    
    # Keep raw data manageable
    if len(RAW_DATA_STORE) > 5000:
        RAW_DATA_STORE.pop(0)
    
    log(f"📨 {direction} from {device_sn}: {len(raw_body)} bytes (Hash: {data_hash[:8]})")
    
    return data_hash

# ------------- DATA PARSING -------------
def parse_device_body(raw_body: str) -> Tuple[str, List[Dict]]:
    """
    Parse eSSL device body and extract:
    1. Device SN (from body or headers)
    2. Attendance records
    """
    device_sn = "UNKNOWN"
    records = []
    
    # Try to find SN in body
    lines = [line.strip() for line in raw_body.splitlines() if line.strip()]
    
    for line in lines:
        # Look for SN= pattern
        if 'SN=' in line.upper():
            parts = line.split('=')
            if len(parts) >= 2:
                device_sn = parts[1].strip().split()[0] if ' ' in parts[1] else parts[1].strip()
                break
        
        # Look for tab-separated attendance data
        if '\t' in line and len(line) > 10:
            parts = line.split('\t')
            if len(parts) >= 3:
                try:
                    record = {
                        'user_id': parts[0].strip(),
                        'timestamp': parts[1].strip(),
                        'status': parts[2].strip(),
                        'verification': parts[3].strip() if len(parts) > 3 else '',
                        'workcode': parts[4].strip() if len(parts) > 4 else '',
                        'raw_line': line,
                        'received_at': datetime.now().isoformat()
                    }
                    
                    # Parse timestamp
                    timestamp = record['timestamp']
                    if ' ' in timestamp and 'T' not in timestamp:
                        record['timestamp'] = timestamp.replace(' ', 'T')
                    
                    # Extended Status mapping for eSSL devices
                    status_map = {
                        '0': 'Check-in',
                        '1': 'Check-out',
                        '2': 'Break-out',
                        '3': 'Break-in',
                        '4': 'Overtime-in',
                        '5': 'Overtime-out',
                        '6': 'Check-in (Override)',
                        '7': 'Check-out (Override)',
                        '8': 'Manual Check-in',
                        '9': 'Manual Check-out',
                        '10': 'Door Open',
                        '11': 'Door Close',
                        '12': 'Access Granted',
                        '13': 'Access Denied',
                        '14': 'Alarm On',
                        '15': 'Alarm Off',
                        '255': 'Error'
                    }
                    record['status_text'] = status_map.get(record['status'], f"Status-{record['status']}")
                    
                    # Format for display
                    if 'T' in record['timestamp']:
                        time_parts = record['timestamp'].split('T')
                        record['display_date'] = time_parts[0]
                        record['display_time'] = time_parts[1][:8] if len(time_parts) > 1 else ''
                    else:
                        record['display_date'] = record['timestamp'][:10]
                        record['display_time'] = record['timestamp'][11:19] if len(record['timestamp']) > 19 else ''
                    
                    records.append(record)
                    
                except Exception as e:
                    log(f"Error parsing attendance line: {e}", "ERROR")
    
    return device_sn, records

def extract_device_info_from_query(params: Dict) -> Dict:
    """Extract device info from query parameters"""
    device_info = {}
    
    for key, value in params.items():
        if isinstance(value, list):
            value = value[0] if value else ""
        
        if key.upper() == 'SN':
            device_info['sn'] = value
        elif key.upper() == 'DEVICENAME':
            device_info['device_name'] = value
        elif key.upper() == 'IP':
            device_info['ip_address'] = value
        elif key.upper() == 'FIRMWARE':
            device_info['firmware'] = value
        elif 'TIME' in key.upper():
            device_info[key] = value
        elif 'DATE' in key.upper():
            device_info[key] = value
        else:
            device_info[key] = value
    
    return device_info

# ------------- DEVICE COMMUNICATION -------------
async def handle_device_request(request: Request, endpoint: str = ""):
    """Handle all device requests"""
    client_ip = get_client_ip(request)
    
    # Get raw body
    raw_body = (await request.body()).decode('utf-8', errors='ignore')
    
    # Parse query parameters
    query_params = dict(request.query_params)
    
    # Store raw data first
    raw_hash = store_raw_data("UNKNOWN", raw_body, "INCOMING", client_ip)
    
    # Try to determine device SN
    device_sn = "UNKNOWN"
    
    # Check query params for SN
    if 'SN' in query_params:
        device_sn = query_params['SN']
        if isinstance(device_sn, list):
            device_sn = device_sn[0]
    
    # Parse body for SN if not in query
    if device_sn == "UNKNOWN":
        parsed_sn, _ = parse_device_body(raw_body)
        if parsed_sn != "UNKNOWN":
            device_sn = parsed_sn
    
    # If still unknown, use IP as identifier
    if device_sn == "UNKNOWN" and client_ip != "unknown":
        device_sn = f"IP-{client_ip.replace('.', '-')}"
    
    # Update device info
    device = get_or_create_device(device_sn, client_ip)
    update_device_last_seen(device_sn, client_ip)
    
    # Update device parameters from query
    device_info = extract_device_info_from_query(query_params)
    if device_info:
        if 'device_name' in device_info:
            device['device_name'] = device_info['device_name']
        elif device_sn and ('560212' in device_sn or 'BOCK' in device_sn.upper()):
            # Special handling for device with specific pattern
            if 'BOCK' in device_sn.upper():
                device['device_name'] = device_sn
            else:
                # Try to extract from SN format
                if '_' in device_sn:
                    device['device_name'] = device_sn.split('_')[0]
        
        # Store all parameters
        device['params'].update(device_info)
        device['params']['last_updated'] = datetime.now().isoformat()
    
    # Parse attendance records from body
    _, records = parse_device_body(raw_body)
    
    # Add records to database
    new_records = 0
    for record in records:
        # Add device info to record
        record['device_sn'] = device_sn
        record['device_name'] = device.get('device_name', f"Device-{device_sn[-6:]}")
        record['raw_data_hash'] = raw_hash
        
        # Check for duplicates
        record_key = f"{device_sn}_{record['user_id']}_{record['timestamp']}_{record['status']}"
        is_duplicate = any(
            f"{r.get('device_sn', '')}_{r['user_id']}_{r['timestamp']}_{r['status']}" == record_key
            for r in ATTENDANCE_RECORDS[-1000:]  # Check last 1000 records for duplicates
        )
        
        if not is_duplicate:
            ATTENDANCE_RECORDS.append(record)
            device['records_count'] += 1
            new_records += 1
            log(f"✓ {record['status_text']}: User {record['user_id']} on {device_sn}")
    
    # Update raw data entry with device SN
    for entry in RAW_DATA_STORE[-10:]:  # Update recent entries
        if entry['id'] == raw_hash:
            entry['device_sn'] = device_sn
            break
    
    if new_records > 0:
        log(f"📊 Added {new_records} new records from {device_sn} (Total: {len(ATTENDANCE_RECORDS)})")
    
    # Save data periodically
    if len(ATTENDANCE_RECORDS) % 10 == 0 or new_records > 0:
        save_all_data()
    
    return device_sn, new_records

# ------------- FASTAPI ROUTES -------------

@app.post("/iclock/cdata.aspx")
async def device_data_endpoint(request: Request):
    """Main endpoint for device to send attendance data"""
    device_sn, new_records = await handle_device_request(request, "cdata")
    
    # Get pending commands for this device
    response_commands = []
    if device_sn in COMMAND_QUEUE and COMMAND_QUEUE[device_sn]:
        response_commands = COMMAND_QUEUE[device_sn].copy()
        COMMAND_QUEUE[device_sn].clear()
    
    # Always request more data if available
    if not response_commands:
        response_commands = ["GET ATTLOG ALL"]
    
    # Combine commands
    response_text = "\n".join(response_commands)
    
    # Log what we're sending back
    if response_commands:
        log(f"📤 Responding to {device_sn}: {response_commands}")
        store_raw_data(device_sn, response_text, "OUTGOING", get_client_ip(request))
    
    return PlainTextResponse(response_text)

@app.get("/iclock/getrequest.aspx")
async def device_command_endpoint(request: Request):
    """Device pulls commands from here"""
    client_ip = get_client_ip(request)
    query_params = dict(request.query_params)
    
    # Get device SN from query
    device_sn = query_params.get('SN', 'UNKNOWN')
    if isinstance(device_sn, list):
        device_sn = device_sn[0]
    
    if device_sn == "UNKNOWN":
        device_sn = f"IP-{client_ip.replace('.', '-')}"
    
    # Update device
    device = get_or_create_device(device_sn, client_ip)
    update_device_last_seen(device_sn, client_ip)
    
    # Check for pending commands
    response_commands = []
    if device_sn in COMMAND_QUEUE and COMMAND_QUEUE[device_sn]:
        response_commands = COMMAND_QUEUE[device_sn].copy()
        COMMAND_QUEUE[device_sn].clear()
    
    # Default response - request ALL attendance data
    if not response_commands:
        response_commands = ["GET ATTLOG ALL"]
    
    # Combine commands
    response_text = "\n".join(response_commands)
    
    # Store outgoing command as raw data
    store_raw_data(device_sn, response_text, "OUTGOING", client_ip)
    log(f"📤 Sending to {device_sn}: {response_commands}")
    
    return PlainTextResponse(response_text)

@app.get("/iclock/registry.aspx")
async def device_registration(request: Request):
    """Device registration endpoint"""
    device_sn, _ = await handle_device_request(request, "registry")
    return PlainTextResponse("OK")

@app.post("/iclock/registry.aspx")
async def device_registration_post(request: Request):
    """Device registration POST endpoint"""
    device_sn, _ = await handle_device_request(request, "registry-post")
    return PlainTextResponse("OK")

@app.get("/")
async def dashboard(request: Request):
    """Main dashboard"""
    # Calculate device status
    for device in DEVICES.values():
        last_seen = datetime.strptime(device['last_seen'], "%Y-%m-%d %H:%M:%S")
        device['last_seen_seconds'] = (datetime.now() - last_seen).total_seconds()
    
    # Get recent raw data (last 20)
    recent_raw = []
    for entry in RAW_DATA_STORE[-20:]:
        recent_raw.append({
            'timestamp': datetime.fromisoformat(entry['timestamp']).strftime("%H:%M:%S"),
            'device_sn': entry['device_sn'],
            'length': entry['length'],
            'hex_preview': ' '.join(f"{ord(c):02x}" for c in entry['raw_data'][:50]),
            'ascii_preview': ''.join(c if 32 <= ord(c) <= 126 else '.' for c in entry['raw_data'][:100])
        })
    
    # Get recent attendance (last 50)
    recent_attendance = ATTENDANCE_RECORDS[-50:]
    
    # Format total data size
    total_data_size = f"{TOTAL_DATA_BYTES / 1024:.1f} KB" if TOTAL_DATA_BYTES < 1048576 else f"{TOTAL_DATA_BYTES / 1048576:.1f} MB"
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "total_records": len(ATTENDANCE_RECORDS),
        "live_records": len([r for r in ATTENDANCE_RECORDS 
                           if datetime.fromisoformat(r['received_at'].replace('Z', '+00:00')) > datetime.now() - timedelta(hours=24)]),
        "devices": list(DEVICES.values()),
        "online_devices": len([d for d in DEVICES.values() if d['last_seen_seconds'] < 300]),
        "total_data_size": total_data_size,
        "total_data_bytes": TOTAL_DATA_BYTES,
        "total_comms": TOTAL_COMMUNICATIONS,
        "recent_raw_data": recent_raw,
        "recent_attendance": recent_attendance,
        "logs": SYSTEM_LOGS[-100:],
        "server_url": str(request.base_url).rstrip('/'),
        "last_update_time": datetime.now().strftime("%H:%M:%S"),
        "now": datetime.now()
    })

# ------------- API ENDPOINTS -------------

@app.get("/api/devices")
async def get_devices():
    """Get all devices"""
    return JSONResponse({
        "devices": DEVICES,
        "count": len(DEVICES),
        "online": len([d for d in DEVICES.values() if d['last_seen_seconds'] < 300])
    })

@app.get("/api/device/{device_sn}")
async def get_device(device_sn: str):
    """Get specific device info"""
    if device_sn not in DEVICES:
        raise HTTPException(status_code=404, detail="Device not found")
    
    device = DEVICES[device_sn].copy()
    last_seen = datetime.strptime(device['last_seen'], "%Y-%m-%d %H:%M:%S")
    device['last_seen_seconds'] = (datetime.now() - last_seen).total_seconds()
    
    return JSONResponse(device)

@app.get("/api/device/{device_sn}/raw-data")
async def get_device_raw_data(device_sn: str, limit: int = 50):
    """Get raw data for specific device"""
    device_data = [entry for entry in RAW_DATA_STORE if entry['device_sn'] == device_sn][-limit:]
    
    # Combine all raw data
    all_raw = "\n".join([entry['raw_data'] for entry in device_data])
    
    return JSONResponse({
        "device_sn": device_sn,
        "count": len(device_data),
        "total_bytes": sum(entry['length'] for entry in device_data),
        "raw_data": all_raw
    })

@app.post("/api/device/{device_sn}/command")
async def send_device_command(device_sn: str, command: str = Form(...)):
    """Send command to specific device"""
    if device_sn not in DEVICES:
        raise HTTPException(status_code=404, detail="Device not found")
    
    if device_sn not in COMMAND_QUEUE:
        COMMAND_QUEUE[device_sn] = []
    
    COMMAND_QUEUE[device_sn].append(command)
    log(f"✅ Command queued for {device_sn}: {command}")
    
    return JSONResponse({"status": "queued", "device": device_sn, "command": command})

@app.post("/api/command/broadcast")
async def broadcast_command(command: str = Form(...)):
    """Broadcast command to all devices"""
    devices_count = 0
    
    for device_sn in DEVICES:
        if device_sn not in COMMAND_QUEUE:
            COMMAND_QUEUE[device_sn] = []
        
        COMMAND_QUEUE[device_sn].append(command)
        devices_count += 1
    
    log(f"📢 Command broadcasted to {devices_count} devices: {command}")
    
    return JSONResponse({
        "status": "broadcasted",
        "command": command,
        "devices_count": devices_count
    })

@app.get("/api/raw-data/recent")
async def get_recent_raw_data(limit: int = 20):
    """Get recent raw data"""
    recent = []
    for entry in RAW_DATA_STORE[-limit:]:
        recent.append({
            'timestamp': datetime.fromisoformat(entry['timestamp']).strftime("%H:%M:%S"),
            'device_sn': entry['device_sn'],
            'length': entry['length'],
            'raw_data': entry['raw_data'],
            'direction': entry['direction']
        })
    
    return JSONResponse(recent)

@app.get("/api/raw-data/{data_hash}")
async def get_raw_data_by_hash(data_hash: str):
    """Get raw data by hash"""
    for entry in RAW_DATA_STORE:
        if entry['id'] == data_hash or entry['hash'] == data_hash:
            return JSONResponse({
                "hash": data_hash,
                "timestamp": entry['timestamp'],
                "device_sn": entry['device_sn'],
                "raw_data": entry['raw_data'],
                "length": entry['length'],
                "direction": entry['direction']
            })
    
    raise HTTPException(status_code=404, detail="Raw data not found")

@app.get("/api/attendance/recent")
async def get_recent_attendance(limit: int = 50):
    """Get recent attendance records"""
    recent = ATTENDANCE_RECORDS[-limit:]
    return JSONResponse(recent)

@app.post("/api/fetch/all")
async def fetch_all_devices():
    """Fetch ALL data from all devices"""
    devices_count = 0
    
    for device_sn in DEVICES:
        if device_sn not in COMMAND_QUEUE:
            COMMAND_QUEUE[device_sn] = []
        
        # Clear existing commands
        COMMAND_QUEUE[device_sn].clear()
        
        # Send commands to get ALL attendance data
        COMMAND_QUEUE[device_sn].extend([
            "GET ATTLOG ALL",  # Get ALL attendance logs
            "GET USERINFO",    # Get user information
            "GET OPTION",      # Get device options
            "DATA",            # Get data buffer
            "CLEAR LOG"        # Clear device logs after fetching
        ])
        devices_count += 1
    
    log(f"🚀 Initiated FULL data fetch for {devices_count} devices")
    
    return JSONResponse({
        "status": "fetch_initiated",
        "devices_count": devices_count,
        "message": f"FULL data fetch initiated for {devices_count} devices (GET ATTLOG ALL)"
    })

@app.post("/api/devices/rescan")
async def rescan_devices():
    """Rescan for devices"""
    old_count = len(DEVICES)
    
    # Mark all devices as offline initially
    for device in DEVICES.values():
        device['status'] = 'offline'
    
    log(f"🔍 Rescanning devices... Found {len(DEVICES)} devices")
    
    return JSONResponse({
        "status": "rescanned",
        "devices_count": len(DEVICES),
        "old_count": old_count
    })

@app.get("/api/logs/recent")
async def get_recent_logs(limit: int = 100):
    """Get recent system logs"""
    return JSONResponse(SYSTEM_LOGS[-limit:])

@app.get("/api/logs/export")
async def export_logs():
    """Export all logs as text file"""
    log_text = "\n".join(SYSTEM_LOGS)
    filename = f"essl_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    # Save temporary file
    temp_file = DATA_DIR / filename
    with open(temp_file, 'w') as f:
        f.write(log_text)
    
    return FileResponse(
        path=temp_file,
        filename=filename,
        media_type='text/plain'
    )

@app.delete("/api/logs/clear")
async def clear_system_logs():
    """Clear system logs"""
    SYSTEM_LOGS.clear()
    log("🗑️ System logs cleared")
    save_all_data()
    
    return JSONResponse({"message": "System logs cleared"})

@app.get("/api/export/csv")
async def export_attendance_csv():
    """Export attendance data as CSV"""
    import csv
    from io import StringIO
    
    output = StringIO()
    writer = csv.writer(output)
    
    writer.writerow(["Device SN", "Device Name", "User ID", "Date", "Time", 
                     "Status", "Status Text", "Verification", "Workcode", "Received At"])
    
    for record in ATTENDANCE_RECORDS[-10000:]:  # Last 10k records
        timestamp = record.get('timestamp', '')
        date = timestamp[:10] if timestamp else ''
        time = timestamp[11:19] if len(timestamp) > 19 else ''
        
        writer.writerow([
            record.get('device_sn', ''),
            record.get('device_name', ''),
            record['user_id'],
            date,
            time,
            record['status'],
            record.get('status_text', ''),
            record['verification'],
            record['workcode'],
            record.get('received_at', '')
        ])
    
    content = output.getvalue()
    filename = f"attendance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "text/csv"
        }
    )

# ------------- STARTUP -------------
@app.on_event("startup")
async def startup():
    """Initialize application"""
    load_all_data()
    
    log("🚀 eSSL Multi-Device Monitor v2.1 Started")
    log(f"📱 Loaded {len(DEVICES)} devices")
    log(f"📊 Loaded {len(ATTENDANCE_RECORDS)} attendance records")
    log(f"📨 Total communications: {TOTAL_COMMUNICATIONS} ({TOTAL_DATA_BYTES} bytes)")

# Setup templates
templates = Jinja2Templates(directory="templates")

# Add custom Jinja2 filters
def last_filter(s, n=4):
    return s[-n:] if s else ""

templates.env.filters["last"] = last_filter



