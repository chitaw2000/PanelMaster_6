from flask import Blueprint, request, jsonify
import json, os, urllib.parse, base64, uuid, random, string, subprocess, threading
from datetime import datetime, timedelta

from utils import get_all_servers, db_lock
from core_auto import load_auto_groups
from core_engine import get_safe_delete_cmd, get_safe_add_out_cmd

try:
    from config import USERS_DB, NODES_LIST
except ImportError:
    USERS_DB = "/root/PanelMaster/users_db.json"
    NODES_LIST = "/root/PanelMaster/nodes_list.txt"

api_bp = Blueprint('api_bp', __name__)
MASTER_API_KEY = "My_Super_Secret_VPN_Key_2026"

def get_target_ip(node_id):
    node_key = str(node_id or "").strip()
    if not node_key:
        return None
    node_key_l = node_key.lower()

    nodes = get_all_servers()
    if node_key in nodes and nodes[node_key].get('ip'):
        return str(nodes[node_key]['ip']).strip()
    for nid, ninfo in nodes.items():
        if str(nid).strip().lower() == node_key_l and ninfo.get('ip'):
            return str(ninfo['ip']).strip()
    if os.path.exists(NODES_LIST):
        with open(NODES_LIST, 'r') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                normalized = line.replace('|', ' ').split()
                if not normalized:
                    continue
                nid = str(normalized[0]).strip().lower()
                if nid == node_key_l and len(normalized) >= 2:
                    return normalized[-1]
    return None

# 🚀 အရင်က အလုပ်လုပ်ခဲ့သော ရိုးရှင်းသည့် နောက်ကွယ်မှ SSH Run သည့်စနစ် ပြန်သုံးထားသည်
def fire_ssh_bg(ip, cmd):
    if not ip: return
    safe_cmd = cmd.replace('"', '\\"')
    full_ssh = f'ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@{ip} "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; {safe_cmd}"'
    subprocess.Popen(full_ssh, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def run_ssh_sync(ip, cmd, timeout=20):
    if not ip:
        return False
    try:
        safe_cmd = cmd.replace('"', '\\"')
        full_ssh = f'ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@{ip} "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; {safe_cmd}"'
        res = subprocess.run(full_ssh, shell=True, capture_output=True, text=True, timeout=timeout)
        return res.returncode == 0
    except Exception:
        return False

def collect_usage_delta(ip, username, last_raw_bytes):
    if not ip:
        return 0.0, float(last_raw_bytes or 0.0)
    try:
        cmd_stats = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{ip} '/usr/local/bin/xray api statsquery --server=127.0.0.1:10085'"
        res = subprocess.run(cmd_stats, shell=True, capture_output=True, text=True, timeout=8)
        if not res.stdout:
            return 0.0, float(last_raw_bytes or 0.0)

        stats = json.loads(res.stdout).get("stat", [])
        current_val = 0.0
        for s in stats:
            p = s.get("name", "").split(">>>")
            if len(p) >= 4 and p[0] == "user" and p[1] == username:
                current_val += float(s.get("value", 0))

        last_val = float(last_raw_bytes or 0.0)
        if current_val > last_val:
            return current_val - last_val, current_val
        if current_val < last_val and current_val > 0:
            return current_val, current_val
        return 0.0, current_val
    except Exception:
        return 0.0, float(last_raw_bytes or 0.0)

@api_bp.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, x-api-key'
    return response

@api_bp.route('/conf/<token>.json', methods=['GET', 'OPTIONS'])
def api_get_ssconf(token):
    if request.method == 'OPTIONS': return jsonify({"success": True}), 200
    with db_lock:
        if not os.path.exists(USERS_DB): return jsonify({"error": "DB not found"}), 404
        with open(USERS_DB, 'r') as f: db = json.load(f)
        
    user_info = next((info for info in db.values() if isinstance(info, dict) and info.get('token') == token), None)
    if not user_info or user_info.get('is_blocked', False):
        return jsonify({"error": "Invalid token or key is blocked/expired"}), 403
        
    node_ip = get_target_ip(user_info.get('node'))
    if not node_ip: return jsonify({"error": "Target node offline"}), 500
    
    data = {
        "server": node_ip,
        "server_port": int(user_info.get('port', 10000)),
        "password": user_info.get('uuid'),
        "method": "chacha20-ietf-poly1305",
        "prefix": "\u0016\u0003\u0001\u0005\u00f2\u0001\u0000\u0005\u00ee\u0003\u0003"
    }
    return jsonify(data)

@api_bp.route('/api/active-groups', methods=['GET', 'OPTIONS'])
def api_get_active_groups():
    if request.method == 'OPTIONS': return jsonify({"success": True}), 200
    if request.headers.get('x-api-key') != MASTER_API_KEY:
        return jsonify({"success": False, "error": "Unauthorized Access"}), 401
    
    try:
        groups = load_auto_groups()
        group_list = [{"id": gid, "name": gdata.get("name", gid), "serverCount": len(gdata.get("nodes", {}))} for gid, gdata in groups.items()]
        return jsonify({"success": True, "groups": group_list})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@api_bp.route('/api/generate-keys', methods=['POST', 'OPTIONS'])
def api_generate_keys():
    if request.method == 'OPTIONS': return jsonify({"success": True}), 200
    if request.headers.get('x-api-key') != MASTER_API_KEY:
        return jsonify({"success": False, "error": "Unauthorized Access"}), 401

    req_data = request.get_json(force=True, silent=True)
    if not req_data: return jsonify({"success": False, "error": "Invalid JSON"}), 400

    group_id = req_data.get('masterGroupId')
    raw_username = req_data.get('userName')
    try: total_gb = float(req_data.get('totalGB', 0))
    except: total_gb = 0.0
    expire_date = req_data.get('expireDate', (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))
    
    if not group_id or not raw_username:
        return jsonify({"success": False, "error": "Missing masterGroupId or userName"}), 400

    username = str(raw_username).strip().replace(" ", "_")
    groups = load_auto_groups()
    if group_id not in groups:
        return jsonify({"success": False, "error": "Group not found"}), 404

    from core_auto import find_available_node
    
    with db_lock:
        if os.path.exists(USERS_DB):
            try:
                with open(USERS_DB, 'r') as f: db = json.load(f)
            except: db = {}
        else:
            db = {}

        if username in db:
            return jsonify({"success": False, "error": "User already exists"}), 400

        target_node, _ = find_available_node(group_id, 1, current_db=db)
        if not target_node:
            return jsonify({"success": False, "error": "Limit Reached! No space available."}), 400

        target_ip = get_target_ip(target_node)
        uid = str(uuid.uuid4()).strip()
        token = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(32))
        safe_u = urllib.parse.quote(username)

        max_p = 10000
        for uinfo in db.values():
            if isinstance(uinfo, dict) and uinfo.get('protocol') == 'out':
                try: p = int(uinfo.get('port', 10000))
                except: p = 10000
                if p > max_p: max_p = p
        port = str(max_p + 1)

        api_keys_dict = {} 
        g_nodes = groups[group_id].get("nodes", {})
        
        # Pre-provision mode: group nodes အားလုံးတွင် key တစ်ကြိမ်တည်း add လုပ်ထားမည်
        for nid in g_nodes:
            nip = get_target_ip(nid)
            if not nip: continue
            nip = str(nip).strip()
            
            api_keys_dict[nid] = {
                "server": nip,
                "server_port": int(port),
                "password": str(uid),
                "method": "chacha20-ietf-poly1305",
                "prefix": "\u0016\u0003\u0001\u0005\u00f2\u0001\u0000\u0005\u00ee\u0003\u0003"
            }
            
            cmd_add = f"{get_safe_add_out_cmd(username, uid, port)} ; systemctl restart xray"
            fire_ssh_bg(nip, cmd_add)

        b64_creds_active = base64.urlsafe_b64encode(f"chacha20-ietf-poly1305:{uid}".encode('utf-8')).decode('utf-8').rstrip('=')
        active_key = f"ss://{b64_creds_active}@{target_ip.strip()}:{port}#{safe_u}"

        existing_ids = [int(u.get('key_id', 0)) for u in db.values() if isinstance(u, dict) and str(u.get('key_id', '')).isdigit()]
        next_id = max(existing_ids) + 1 if existing_ids else 1

        db[username] = {
            "node": target_node, "group": group_id, "protocol": "out", "uuid": uid,
            "port": port, "total_gb": total_gb, "expire_date": expire_date,
            "used_bytes": 0, "last_raw_bytes": 0, "is_blocked": False, "is_online": False,
            "key": active_key, "key_id": next_id, "token": token
        }

        with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)

    return jsonify({"success": True, "keys": api_keys_dict, "token": token})

@api_bp.route('/api/webhook/switch', methods=['POST', 'OPTIONS'])
def webhook_switch():
    if request.method == 'OPTIONS': return jsonify({"success": True}), 200
    if request.headers.get('x-api-key') != MASTER_API_KEY:
        return jsonify({"success": False, "error": "Unauthorized Access"}), 401

    req_data = request.get_json(force=True, silent=True)
    if not req_data: return jsonify({"success": False, "error": "Invalid JSON"}), 400

    token = req_data.get('token')
    target_node_raw = str(req_data.get('activeServer', '')).strip()

    if not token or not target_node_raw: 
        return jsonify({"success": False, "error": "Missing token or activeServer"}), 400

    def _norm(s):
        return str(s or "").strip().lower()

    target_node = None
    raw_n = _norm(target_node_raw)
    nodes = get_all_servers()
    for nid, ndata in nodes.items():
        nid_n = _norm(nid)
        name = str(ndata.get('name', '')).strip()
        name_n = _norm(name)
        name_no_auto = name.strip()
        if name_no_auto.startswith("[AUTO]"):
            name_no_auto = name_no_auto.replace("[AUTO]", "", 1).strip()
        name_no_auto_n = _norm(name_no_auto)

        if raw_n in {nid_n, name_n, name_no_auto_n}:
            target_node = nid
            break
            
    if not target_node: return jsonify({"success": False, "error": "Target node not found"}), 404

    new_ip = get_target_ip(target_node)
    if not new_ip: return jsonify({"success": False, "error": "Target node offline"}), 500
    new_ip = str(new_ip).strip()

    with db_lock:
        if not os.path.exists(USERS_DB): return jsonify({"success": False, "error": "DB not found"}), 404
        with open(USERS_DB, 'r') as f: db = json.load(f)
        
        username = next((uname for uname, info in db.items() if isinstance(info, dict) and info.get('token') == token), None)
        if not username: return jsonify({"success": False, "error": "Invalid token"}), 404
        uinfo = db[username]
        
        old_node = uinfo.get('node')
        if old_node == target_node: return jsonify({"success": True, "message": "Already connected"})
        
        old_ip = get_target_ip(old_node)
        old_ip = str(old_ip).strip() if old_ip else None
        
        uid = uinfo.get('uuid')
        port = uinfo.get('port')
        safe_u = urllib.parse.quote(username)
        group_id = uinfo.get('group')
        is_blocked = uinfo.get('is_blocked', False)
        proto = uinfo.get('protocol', 'out')
        
        # Switch မလုပ်ခင် old node ရဲ့ raw counter delta ကိုသာ ယူ (double count မဖြစ်စေရန်)
        delta_bytes, _ = collect_usage_delta(old_ip, username, uinfo.get('last_raw_bytes', 0))
        if delta_bytes > 0:
            uinfo['used_bytes'] = float(uinfo.get('used_bytes', 0)) + float(delta_bytes)

        # New active node အတွက် traffic counter restart
        uinfo['last_raw_bytes'] = 0
        b64_creds = base64.urlsafe_b64encode(f"chacha20-ietf-poly1305:{uid}".encode('utf-8')).decode('utf-8').rstrip('=')
        uinfo['node'] = target_node  
        if proto == 'v2':
            uinfo['key'] = f"vless://{uid}@{new_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
        else:
            uinfo['key'] = f"ss://{b64_creds}@{new_ip}:{port}#{safe_u}"
        
        with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)
        
    # Pre-provision mode: switch တွင် DB/key server သာ ပြောင်းမည် (node sync မလုပ်တော့)
    return jsonify({"success": True, "message": "Successfully switched and synced GB"})

@api_bp.route('/api/user-action', methods=['POST', 'OPTIONS'])
def api_user_action():
    if request.method == 'OPTIONS': return jsonify({"success": True}), 200
    if request.headers.get('x-api-key') != MASTER_API_KEY:
        return jsonify({"success": False, "error": "Unauthorized Access"}), 401

    req_data = request.get_json(force=True, silent=True)
    if not req_data: return jsonify({"success": False, "error": "Invalid JSON"}), 400

    token = req_data.get('token')
    action = req_data.get('action')

    with db_lock:
        if not os.path.exists(USERS_DB): return jsonify({"success": False}), 404
        with open(USERS_DB, 'r') as f: db = json.load(f)
        
        username = next((uname for uname, info in db.items() if isinstance(info, dict) and info.get('token') == token), None)
        if not username: return jsonify({"success": False}), 404

        uinfo = db[username]
        target_node = uinfo.get('node')
        node_ip = get_target_ip(target_node)
        active_ip = str(node_ip).strip() if node_ip else None
        
        port = uinfo.get('port')
        uid = uinfo.get('uuid')
        group_id = uinfo.get('group')

        if action == "suspend": uinfo['is_blocked'] = True
        elif action == "resume": uinfo['is_blocked'] = False
        elif action == "delete": del db[username]

        with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)
        
    groups = load_auto_groups()
    if action == "resume" and group_id:
        g_nodes = groups.get(group_id, {}).get("nodes", {})
    else:
        g_nodes = groups.get(group_id, {}).get("nodes", {}) if group_id else {target_node: {}}

    for nid in g_nodes:
        nip = get_target_ip(nid)
        if not nip: continue
        nip = str(nip).strip()

        if action in ["suspend", "delete"]:
            cmd_del = get_safe_delete_cmd(username, 'out', port)
            cmd_full_del = f"{cmd_del} ; ufw delete allow {port}/tcp >/dev/null 2>&1 || true ; ufw delete allow {port}/udp >/dev/null 2>&1 || true ; systemctl restart xray"
            fire_ssh_bg(nip, cmd_full_del)
        elif action == "resume":
            if group_id:
                cmd_add = f"{get_safe_add_out_cmd(username, uid, port)} ; systemctl restart xray"
                fire_ssh_bg(nip, cmd_add)
            elif nip == active_ip:
                cmd_add = f"{get_safe_add_out_cmd(username, uid, port)} ; systemctl restart xray"
                fire_ssh_bg(nip, cmd_add)

    return jsonify({"success": True})

@api_bp.route('/api/internal/delete-user', methods=['POST', 'OPTIONS'])
def api_internal_delete_user():
    if request.method == 'OPTIONS':
        return jsonify({"success": True}), 200
    if request.headers.get('x-api-key') != MASTER_API_KEY:
        return jsonify({"success": False, "error": "Unauthorized Access"}), 401

    req_data = request.get_json(force=True, silent=True) or {}
    username = str(req_data.get('username', '')).strip()
    token = str(req_data.get('token', '')).strip()

    with db_lock:
        if not os.path.exists(USERS_DB):
            return jsonify({"success": False, "error": "DB not found"}), 404
        with open(USERS_DB, 'r') as f:
            db = json.load(f)

        if not username and token:
            username = next((u for u, i in db.items() if isinstance(i, dict) and i.get('token') == token), '')
        if not username or username not in db:
            return jsonify({"success": False, "error": "User not found"}), 404

        uinfo = db[username]
        group_id = uinfo.get('group')
        target_node = uinfo.get('node')
        port = uinfo.get('port')
        proto = uinfo.get('protocol', 'out')
        del db[username]

        with open(USERS_DB, 'w') as f:
            json.dump(db, f, indent=4)

    groups = load_auto_groups()
    g_nodes = groups.get(group_id, {}).get("nodes", {}) if group_id else {target_node: {}}
    for nid in g_nodes:
        nip = get_target_ip(nid)
        if not nip:
            continue
        nip = str(nip).strip()
        cmd_del = get_safe_delete_cmd(username, proto, port if proto != 'v2' else '443')
        if proto == 'v2':
            cmd_full_del = f"{cmd_del} ; systemctl restart xray"
        else:
            cmd_full_del = f"{cmd_del} ; ufw delete allow {port}/tcp >/dev/null 2>&1 || true ; ufw delete allow {port}/udp >/dev/null 2>&1 || true ; systemctl restart xray"
        fire_ssh_bg(nip, cmd_full_del)

    return jsonify({"success": True, "message": "User deleted"})
