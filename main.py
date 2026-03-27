from flask import Flask, render_template, request, redirect, session, url_for, send_file, jsonify
import json, os, re, subprocess, urllib.parse, base64, threading, time, requests
from datetime import datetime, timedelta

from config import SECRET_KEY, USERS_DB, NODES_LIST, CONFIG_FILE, ADMIN_PASS, load_config, save_config
from utils import get_nodes, get_all_servers, check_live_status, db_lock, AUTO_GROUPS_FILE, NODES_DB
from core_auto import load_auto_groups, save_auto_groups

from core_engine import execute_ssh_bg, get_safe_delete_cmd
from core_monitor import start_background_monitor
from core_node import add_keys, toggle_key, delete_key, bulk_delete_keys, renew_key, edit_key, rebalance_auto_node
from core_ip import get_active_ips

# 🚀 API Blueprint ကို လှမ်းခေါ်ခြင်း
from core_api import api_bp

app = Flask(__name__)
app.secret_key = SECRET_KEY
BACKUP_DIR = "/root/PanelMaster/backups"
MASTER_API_KEY = "My_Super_Secret_VPN_Key_2026"

if not os.path.exists(BACKUP_DIR): 
    os.makedirs(BACKUP_DIR)

# 🚀 API Routes များကို Flask ထဲသို့ ပေါင်းထည့်ခြင်း
app.register_blueprint(api_bp)

start_background_monitor()

@app.before_request
def check_auth():
    if request.path.startswith('/api/') or request.path.startswith('/conf/'):
        return
    allowed_ui_endpoints = ['login', 'static', 'api_stats', 'api_user_ip', 'api_check_ssh', 'api_check_xray']
    if request.endpoint not in allowed_ui_endpoints and not session.get('logged_in'): 
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASS:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/logout')
def logout(): 
    session.clear()
    return redirect(url_for('login'))

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

@app.route('/api/user_ip/<username>')
def api_user_ip(username):
    with db_lock:
        db = {}
        if os.path.exists(USERS_DB):
            with open(USERS_DB, 'r') as f: db = json.load(f)
    
    if username not in db: return jsonify({"status": "error", "msg": "User not found"})
    
    uinfo = db[username]
    node_id = uinfo.get('node')
    port = uinfo.get('port', '443')
    proto = uinfo.get('protocol', 'v2')
    
    node_ip = get_target_ip(node_id)
    if not node_ip: return jsonify({"status": "error", "msg": "Node offline"})
    
    ips_info = get_active_ips(node_ip, port, proto, username)
    return jsonify({"status": "success", "data": ips_info})

@app.route('/fix_node_logs/<node_id>', methods=['POST'])
def fix_node_logs(node_id):
    ip = get_target_ip(node_id)
    if ip:
        cmds = [
            "mkdir -p /var/log/xray",
            "touch /var/log/xray/access.log",
            "chmod 777 /var/log/xray/access.log",
            "grep -q 'access.log' /usr/local/etc/xray/config.json || sed -i 's/\"log\": {/\"log\": {\\n    \"access\": \"\\/var\\/log\\/xray\\/access.log\",/g' /usr/local/etc/xray/config.json",
            "systemctl restart xray"
        ]
        execute_ssh_bg(ip, cmds)
    return redirect(request.referrer)

@app.route('/set_node_health/<node_id>', methods=['POST'])
def set_node_health(node_id):
    health = request.form.get('health', 'green')
    with db_lock:
        ndb = {}
        if os.path.exists(NODES_DB):
            try:
                with open(NODES_DB, 'r') as f: ndb = json.load(f)
            except: pass
        if node_id not in ndb: ndb[node_id] = {"used_bytes": 0, "limit_tb": 0, "health": "green"}
        ndb[node_id]["health"] = health
        with open(NODES_DB, 'w') as f: json.dump(ndb, f)
    return redirect(request.referrer)

@app.route('/set_node_traffic/<node_id>', methods=['POST'])
def set_node_traffic(node_id):
    try: tb = float(request.form.get('limit_tb', 0))
    except: tb = 0.0
    with db_lock:
        ndb = {}
        if os.path.exists(NODES_DB):
            try:
                with open(NODES_DB, 'r') as f: ndb = json.load(f)
            except: pass
        if node_id not in ndb: ndb[node_id] = {"used_bytes": 0, "limit_tb": 0, "health": "green"}
        ndb[node_id]["limit_tb"] = tb
        with open(NODES_DB, 'w') as f: json.dump(ndb, f)
    return redirect(request.referrer)

@app.route('/reset_node_traffic/<node_id>', methods=['POST'])
def reset_node_traffic(node_id):
    with db_lock:
        ndb = {}
        if os.path.exists(NODES_DB):
            try:
                with open(NODES_DB, 'r') as f: ndb = json.load(f)
            except: pass
        if node_id in ndb:
            ndb[node_id]["used_bytes"] = 0
            with open(NODES_DB, 'w') as f: json.dump(ndb, f)
    return redirect(request.referrer)

def get_node_backups():
    backups = {}
    if os.path.exists(BACKUP_DIR):
        for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if f.endswith('.json') and f.startswith("backup_"):
                parts = f.split('_')
                if len(parts) >= 3:
                    nid = parts[1]
                    if nid not in backups: backups[nid] = []
                    path = os.path.join(BACKUP_DIR, f)
                    size = os.path.getsize(path) / 1024
                    ctime = datetime.fromtimestamp(os.path.getctime(path)).strftime('%Y-%m-%d %I:%M %p')
                    backups[nid].append({"filename": f, "size": f"{size:.1f} KB", "time": ctime})
    return backups

@app.route('/')
def dashboard():
    nodes = get_nodes()
    auto_groups = load_auto_groups()
    db = {}
    ndb = {}
    with db_lock:
        if os.path.exists(USERS_DB):
            try:
                with open(USERS_DB, 'r') as f: db = json.load(f)
            except: pass
        if os.path.exists(NODES_DB):
            try:
                with open(NODES_DB, 'r') as f: ndb = json.load(f)
            except: pass
                
    config = load_config()
    active_users = check_live_status(db)
    node_stats = []
    group_stats = []
    
    node_used_bytes = {}
    group_used_bytes = {}
    
    for uname, uinfo in db.items():
        if not isinstance(uinfo, dict): continue
        nid = uinfo.get('node')
        gid = uinfo.get('group')
        try: u_bytes = float(uinfo.get('used_bytes', 0))
        except: u_bytes = 0.0
        if nid: node_used_bytes[nid] = node_used_bytes.get(nid, 0) + u_bytes
        if gid: group_used_bytes[gid] = group_used_bytes.get(gid, 0) + u_bytes
    
    all_servers = get_all_servers()
    sick_nodes = {'blue': [], 'yellow': [], 'orange': [], 'red': []}
    sick_count = 0
    for nid, info in all_servers.items():
        h = ndb.get(nid, {}).get("health", "green")
        if h in sick_nodes:
            sick_nodes[h].append({"id": nid, "name": info.get('name', nid), "ip": info.get('ip', '')})
            sick_count += 1
            
    for nid, info in nodes.items():
        total_count = sum(1 for i in db.values() if isinstance(i, dict) and i.get('node') == nid and not i.get('group'))
        live_count = sum(1 for uname, i in db.items() if isinstance(i, dict) and i.get('node') == nid and not i.get('group') and uname in active_users and not i.get('is_blocked'))
        
        ninfo = ndb.get(nid, {})
        limit_tb = float(ninfo.get("limit_tb", 0))
        used_gb = float(node_used_bytes.get(nid, 0)) / (1024**3)
        limit_gb = limit_tb * 1024
        is_alarm = limit_gb > 0 and used_gb >= limit_gb
        health = ninfo.get("health", "green")

        node_stats.append({
            "id": nid, "name": info.get('name', nid), "ip": info.get('ip', ''), 
            "total": total_count, "live": live_count, "disabled": nid in config.get('disabled_nodes', []),
            "used_gb": used_gb, "limit_tb": limit_tb, "is_alarm": is_alarm, "health": health
        })
        
    for gid, gdata in auto_groups.items():
        limit = gdata.get("limit", 30)
        g_nodes = gdata.get("nodes", {})
        g_keys = sum(1 for i in db.values() if isinstance(i, dict) and i.get("group") == gid)
        g_used_gb = group_used_bytes.get(gid, 0) / (1024**3)
        api_domain = gdata.get("api_domain", "")
        group_stats.append({"id": gid, "name": gdata.get("name", gid), "limit": limit, "api_domain": api_domain, "node_count": len(g_nodes), "total_keys": g_keys, "used_gb": g_used_gb})

    raw_backups = get_node_backups()
    custom_backups = {}
    auto_backups = {}
    orphaned_backups = {}
    
    auto_nids_map = {}
    for gid, gdata in auto_groups.items():
        auto_backups[gid] = {"name": gdata.get('name', gid), "nodes": {}}
        for nid in gdata.get('nodes', {}).keys():
            auto_nids_map[nid] = gid
            
    for nid, files in raw_backups.items():
        if nid in nodes:
            custom_backups[nid] = {"name": nodes[nid].get('name', nid), "files": files}
        elif nid in auto_nids_map:
            gid = auto_nids_map[nid]
            auto_backups[gid]["nodes"][nid] = files
        else:
            orphaned_backups[nid] = files
            
    auto_backups = {k: v for k, v in auto_backups.items() if v["nodes"]}

    return render_template('dashboard.html', nodes=node_stats, groups=group_stats, config=config, custom_backups=custom_backups, auto_backups=auto_backups, orphaned_backups=orphaned_backups, sick_nodes=sick_nodes, sick_count=sick_count)

@app.route('/add_auto_group', methods=['POST'])
def add_auto_group():
    gid = request.form.get('group_id', '').strip().replace(" ", "_")
    gname = request.form.get('group_name', '').strip()
    limit = int(request.form.get('limit', 30))
    api_domain = request.form.get('api_domain', '').strip()
    
    if gid and gname:
        groups = load_auto_groups()
        groups[gid] = {"name": gname, "limit": limit, "api_domain": api_domain, "nodes": {}}
        save_auto_groups(groups)
    return redirect(url_for('dashboard'))

@app.route('/delete_auto_group/<group_id>', methods=['POST'])
def delete_auto_group(group_id):
    groups = load_auto_groups()
    if group_id in groups:
        del groups[group_id]
        save_auto_groups(groups)
    return redirect(url_for('dashboard'))

@app.route('/group/<group_id>')
def group_view(group_id):
    groups = load_auto_groups()
    if group_id not in groups: 
        return redirect(url_for('dashboard'))
        
    group = groups[group_id]
    db = {}
    ndb = {}
    with db_lock:
        if os.path.exists(USERS_DB):
            with open(USERS_DB, 'r') as f: db = json.load(f)
        if os.path.exists(NODES_DB):
            try:
                with open(NODES_DB, 'r') as f: ndb = json.load(f)
            except: pass
            
    active_users = check_live_status(db)
    users = []
    server_stats = []
    g_nodes = group.get("nodes", {})
    counts = {nid: 0 for nid in g_nodes.keys()}
    
    node_used_bytes = {}
    group_total_bytes = 0
    current_date_str = datetime.now().strftime("%Y-%m-%d")
    
    db_changed = False
    cmds_by_ip = {}
    
    for uname, info in db.items():
        if not isinstance(info, dict): continue
        if info.get('group') == group_id:
            nid = info.get('node')
            node_ip = get_target_ip(nid)
            
            if node_ip:
                uid = info.get('uuid')
                port = info.get('port')
                proto = info.get('protocol', 'v2')
                safe_u = urllib.parse.quote(uname)
                
                if proto == 'v2':
                    expected_key = f"vless://{uid}@{node_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
                    cmd = f"/usr/local/bin/v2ray-node-add-vless {uname} {uid}"
                else:
                    credentials = f"chacha20-ietf-poly1305:{uid}"
                    b64_creds = base64.urlsafe_b64encode(credentials.encode('utf-8')).decode('utf-8').rstrip('=')
                    expected_key = f"ss://{b64_creds}@{node_ip}:{port}#{safe_u}"
                    cmd = f"/usr/local/bin/v2ray-node-add-out {uname} {uid} {port} ; ufw allow {port}/tcp >/dev/null 2>&1 || true ; ufw allow {port}/udp >/dev/null 2>&1 || true"
                    
                if info.get('key') != expected_key:
                    info['key'] = expected_key
                    db_changed = True
                    if not info.get('is_blocked', False):
                        cmds_by_ip.setdefault(node_ip, []).append(cmd)
            
            info['used_bytes'] = float(info.get('used_bytes', 0))
            info['total_gb'] = float(info.get('total_gb', 0))
            info['used_gb_str'] = f"{(info['used_bytes'] / (1024**3)):.2f}"
            info['username'] = uname
            info['actual_key'] = info.get('key') or "No Key Found"
            info['is_active'] = uname in active_users and not info.get('is_blocked')
            info['protocol_label'] = "VLESS" if info.get('protocol') == 'v2' else "Outline SS"
            
            exp_str = info.get('expire_date')
            is_expired = True if (exp_str and current_date_str > exp_str) else False
            
            if is_expired: info['status_label'] = "Expired"
            elif info.get('is_blocked'): info['status_label'] = "Blocked"
            elif info['is_active']: info['status_label'] = "Online"
            else: info['status_label'] = "Offline"
                
            users.append(info)
            if nid in counts: counts[nid] += 1
            if nid: node_used_bytes[nid] = node_used_bytes.get(nid, 0) + info['used_bytes']
            group_total_bytes += info['used_bytes']
            
    if db_changed:
        with db_lock:
            with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)
    for ip, cmds in cmds_by_ip.items():
        prefix = "systemctl() { true; }; export -f systemctl; "
        suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
        execute_ssh_bg(ip, [prefix + " ; ".join(cmds) + suffix])
            
    users = sorted(users, key=lambda x: int(x.get('key_id', 0)))
    group_used_gb = group_total_bytes / (1024**3)
            
    for nid, ndata in g_nodes.items():
        if isinstance(ndata, dict):
            nip = str(ndata.get("ip")).strip()
            limit = int(ndata.get("limit", group.get("limit", 30)))
        else:
            nip = str(ndata).strip()
            limit = int(group.get("limit", 30))
            
        ninfo = ndb.get(nid, {})
        limit_tb = float(ninfo.get("limit_tb", 0))
        used_gb = node_used_bytes.get(nid, 0) / (1024**3)
        limit_gb = limit_tb * 1024
        is_alarm = limit_tb > 0 and used_gb >= limit_gb
        health = ninfo.get("health", "green")
        
        server_stats.append({"id": nid, "ip": nip, "count": counts[nid], "limit": limit, "used_gb": used_gb, "limit_tb": limit_tb, "is_alarm": is_alarm, "health": health})
        
    return render_template('group.html', group_id=group_id, group=group, users=users, server_stats=server_stats, group_used_gb=group_used_gb)

def sync_new_node_to_subpanel(group_id, new_node_id, new_node_ip):
    time.sleep(3) 
    try:
        with db_lock:
            if not os.path.exists(USERS_DB): return
            with open(USERS_DB, 'r') as f: db = json.load(f)

        user_keys = {}
        for uname, uinfo in db.items():
            if isinstance(uinfo, dict) and uinfo.get('group') == group_id and uinfo.get('token'):
                uid = uinfo.get('uuid')
                port = uinfo.get('port')
                proto = uinfo.get('protocol', 'v2')
                safe_u = urllib.parse.quote(uname)

                if proto == 'v2':
                    k = f"vless://{uid}@{new_node_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
                else:
                    k = {
                        "server": str(new_node_ip),
                        "server_port": int(port),
                        "password": str(uid),
                        "method": "chacha20-ietf-poly1305",
                        "prefix": "\u0016\u0003\u0001\u0005\u00f2\u0001\u0000\u0005\u00ee\u0003\u0003"
                    }

                user_keys[uinfo['token']] = k

        if not user_keys: return 

        payload = {
            "masterGroupId": group_id,
            "newServerName": new_node_id,
            "userKeys": user_keys
        }
        
        headers = {"Content-Type": "application/json", "x-api-key": MASTER_API_KEY}
        requests.post("http://167.172.91.222:4000/api/internal/sync-new-server", json=payload, headers=headers, timeout=10)
        
    except Exception as e:
        print(f"Sync New Server Error: {e}")

@app.route('/add_server_to_group/<group_id>', methods=['POST'])
def add_server_to_group(group_id):
    nid = request.form.get('node_id', '').strip().replace(" ", "_")
    nip = request.form.get('node_ip', '').strip()
    limit = int(request.form.get('limit', 30))
    groups = load_auto_groups()
    nodes = get_all_servers()
    
    if nid in nodes:
        return f"<script>alert('Error: Server ID [{nid}] already exists!'); window.history.back();</script>"
        
    if group_id in groups and nid and nip:
        groups[group_id]["nodes"][nid] = {"ip": nip, "limit": limit}
        save_auto_groups(groups)
        threading.Thread(target=sync_new_node_to_subpanel, args=(group_id, nid, nip), daemon=True).start()
        
    return redirect(f'/group/{group_id}?newly_added={nid}')

@app.route('/delete_server_from_group/<group_id>/<node_id>', methods=['POST'])
def delete_server_from_group(group_id, node_id):
    groups = load_auto_groups()
    node_ip = None
    if group_id in groups and node_id in groups[group_id]["nodes"]:
        ndata = groups[group_id]["nodes"][node_id]
        node_ip = str(ndata.get("ip")).strip() if isinstance(ndata, dict) else str(ndata).strip()
        del groups[group_id]["nodes"][node_id]
        save_auto_groups(groups)
        
    if node_ip:
        with db_lock:
            if os.path.exists(USERS_DB):
                with open(USERS_DB, 'r') as f: 
                    db = json.load(f)
                users_to_delete = [u for u, info in db.items() if info.get('node') == node_id]
        if users_to_delete: 
            bulk_delete_keys(users_to_delete)
            
    return redirect(f'/group/{group_id}')

@app.route('/edit_group_limit/<group_id>', methods=['POST'])
def edit_group_limit(group_id):
    new_limit = int(request.form.get('limit', 30))
    success, msg = rebalance_auto_node(group_id, new_limit)
    if not success: 
        return f"<script>alert('{msg}'); window.location.href='/group/{group_id}';</script>"
    return redirect(f'/group/{group_id}')

@app.route('/edit_server_limit/<group_id>/<node_id>', methods=['POST'])
def edit_server_limit(group_id, node_id):
    new_limit = int(request.form.get('limit', 30))
    success, msg = rebalance_auto_node(group_id, new_limit, specific_node=node_id)
    if not success: 
        return f"<script>alert('{msg}'); window.location.href='/group/{group_id}';</script>"
    return redirect(f'/group/{group_id}')

@app.route('/add_user_auto', methods=['POST'])
def add_user_auto():
    gid = request.form.get('group_id', '').strip()
    mode = request.form.get('creation_mode', 'single')
    
    raw_usernames = []
    if mode == 'single': 
        raw_usernames = [request.form.get('single_username', '')]
    elif mode == 'list': 
        raw_usernames = re.split(r'[,\n\r]+', request.form.get('list_usernames', ''))
    elif mode == 'pattern':
        base = request.form.get('base_name', '').strip()
        try: start = int(request.form.get('start_num') or 1)
        except: start = 1
        try: qty = int(request.form.get('qty') or 1)
        except: qty = 1
        raw_usernames = [f"{base}{start+i}" for i in range(qty)]

    try: gb = float(request.form.get('total_gb') or 0)
    except: gb = 0.0
    try: days = int(request.form.get('expire_days') or 30)
    except: days = 30
    
    proto = request.form.get('protocol', 'v2')

    success, msg = add_keys(None, gid, raw_usernames, gb, days, proto, is_auto=True)
    if not success: 
        return f"<script>alert('{msg}'); window.history.back();</script>"
    return redirect(f'/group/{gid}')

# 🚀 ဒီလမ်းကြောင်းလေးက ပြဿနာရဲ့ အဓိက တရားခံပဲ! 🚀
@app.route('/node/<node_id>')
def node_view(node_id):
    nodes = get_all_servers()
    if node_id not in nodes: 
        return redirect(url_for('dashboard'))
        
    node_info = nodes[node_id]
    node_ip = str(node_info.get('ip', '')).strip()
    
    db = {}
    ndb = {}
    with db_lock:
        if os.path.exists(USERS_DB):
            try:
                with open(USERS_DB, 'r') as f: db = json.load(f)
            except: pass
        if os.path.exists(NODES_DB):
            try:
                with open(NODES_DB, 'r') as f: ndb = json.load(f)
            except: pass
            
    config = load_config()
    active_users = check_live_status(db)
    auto_groups = load_auto_groups() # 🚀 Group များကို လှမ်းခေါ်မည်
    users = []
    node_used_bytes = 0
    current_date_str = datetime.now().strftime("%Y-%m-%d")
    
    db_changed = False
    cmds_to_sync = []
    
    for uname, info in db.items():
        if not isinstance(info, dict): continue 
        
        # 🚀 ညိုကီ လိုချင်သည့်အတိုင်း Group ထဲမှ ဆာဗာတိုင်းတွင် လိုက်ပြရန် စစ်ဆေးခြင်း
        user_node = info.get('node')
        user_group = info.get('group')
        
        is_active_node = (user_node == node_id)
        belongs_to_node = is_active_node
        
        # သတ်မှတ်ထားသော Active Node မဟုတ်ပါက၊ ၎င်း၏ Group ထဲတွင် ဤဆာဗာ ပါမပါ စစ်ဆေးမည်
        if not belongs_to_node and user_group and user_group in auto_groups:
            if node_id in auto_groups[user_group].get("nodes", {}):
                belongs_to_node = True

        # 🚀 ဤဆာဗာ (သို့) ဤဆာဗာပါဝင်သော Group မှ User ဖြစ်လျှင် မျက်နှာပြင်တွင် ပြပေးမည်
        if belongs_to_node:
            uid = info.get('uuid')
            port = info.get('port')
            proto = info.get('protocol', 'v2')
            safe_u = urllib.parse.quote(uname)
            
            # 🚀 ဝင်ကြည့်နေသော ဆာဗာ၏ IP ဖြင့်သာ Key ကို အတိအကျ ပြောင်းထုတ်ပေးမည်
            if proto == 'v2':
                expected_key = f"vless://{uid}@{node_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
                cmd = f"/usr/local/bin/v2ray-node-add-vless {uname} {uid}"
            else:
                credentials = f"chacha20-ietf-poly1305:{uid}"
                b64_creds = base64.urlsafe_b64encode(credentials.encode('utf-8')).decode('utf-8').rstrip('=')
                expected_key = f"ss://{b64_creds}@{node_ip}:{port}#{safe_u}"
                cmd = f"/usr/local/bin/v2ray-node-add-out {uname} {uid} {port} ; ufw allow {port}/tcp >/dev/null 2>&1 || true ; ufw allow {port}/udp >/dev/null 2>&1 || true"
                
            # Database တွင် အပြောင်းအလဲလုပ်ခြင်းကို Active ဖြစ်သော ပင်မဆာဗာ (၁) ခုတည်းအတွက်သာ လုပ်မည်
            if is_active_node:
                if info.get('key') != expected_key:
                    info['key'] = expected_key
                    db_changed = True
                    if not info.get('is_blocked', False):
                        cmds_to_sync.append(cmd)
            
            # 🚀 UI တွင်ပြရန်အတွက် သီးသန့် Copy ကူး၍ ပြင်ဆင်မည် (Main DB အား မထိခိုက်စေရန်)
            display_info = info.copy()
            display_info['used_bytes'] = float(display_info.get('used_bytes', 0))
            display_info['total_gb'] = float(display_info.get('total_gb', 0))
            display_info['used_gb_str'] = f"{(display_info['used_bytes'] / (1024**3)):.2f}"
            display_info['username'] = uname
            
            # 🚀 အဓိက - ဤ Node အတွက် အတိအကျ ပြောင်းလဲထားသော Key ကို မျက်နှာပြင်တွင် ပြမည်
            display_info['actual_key'] = expected_key
            
            display_info['is_active'] = uname in active_users and not display_info.get('is_blocked')
            display_info['protocol_label'] = "VLESS" if display_info.get('protocol') == 'v2' else "Outline SS"
            
            exp_str = display_info.get('expire_date')
            is_expired = True if (exp_str and current_date_str > exp_str) else False
            
            if is_expired: display_info['status_label'] = "Expired"
            elif display_info.get('is_blocked'): display_info['status_label'] = "Blocked"
            elif display_info['is_active']: display_info['status_label'] = "Online"
            else: display_info['status_label'] = "Offline"
                
            if not is_active_node:
                display_info['status_label'] += " (Synced)"
                
            users.append(display_info)
            
            # GB အသုံးပြုမှုကို Active ဖြစ်သော ဆာဗာအတွက်သာ ပေါင်းထည့်မည် (၂ ခါ မထပ်စေရန်)
            if is_active_node:
                node_used_bytes += display_info['used_bytes']
            
    if db_changed:
        with db_lock:
            with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)
    if cmds_to_sync and node_ip:
        prefix = "systemctl() { true; }; export -f systemctl; "
        suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
        execute_ssh_bg(node_ip, [prefix + " ; ".join(cmds_to_sync) + suffix])
            
    ninfo = ndb.get(node_id, {})
    limit_tb = float(ninfo.get("limit_tb", 0))
    used_gb = node_used_bytes / (1024**3)
    limit_gb = limit_tb * 1024
    is_alarm = limit_tb > 0 and used_gb >= limit_gb
    health = ninfo.get("health", "green")
            
    other_nodes = [nid for nid in nodes.keys() if nid != node_id]
    
    return render_template('node.html', node_id=node_id, node_name=node_info.get('name', ''), node_ip=node_ip, users=users, other_nodes=other_nodes, config=config, used_gb=used_gb, limit_tb=limit_tb, is_alarm=is_alarm, health=health)

@app.route('/add_node', methods=['POST'])
def add_node():
    n_id = request.form.get('node_id', '').strip().replace(" ", "_")
    n_name = request.form.get('node_name', '').strip()
    n_ip = request.form.get('node_ip', '').strip()
    
    if n_id and n_name and n_ip:
        nodes = get_all_servers()
        if n_id in nodes:
            return f"<script>alert('Error: Node ID [{n_id}] already exists!'); window.history.back();</script>"
            
        if not os.path.exists(NODES_LIST):
            with open(NODES_LIST, 'w') as f: 
                f.write("")
                
        with open(NODES_LIST, 'a') as f: 
            f.write(f"\n{n_id}|{n_name}|{n_ip}")
            
    return redirect(f"/node/{n_id}?newly_added={n_id}")

@app.route('/delete_node/<node_id>', methods=['POST'])
def delete_node(node_id):
    nodes = get_all_servers()
    if node_id in nodes:
        node_ip = str(nodes[node_id].get('ip')).strip()
        if node_ip: 
            execute_ssh_bg(node_ip, ["systemctl stop xray"])
    
    if os.path.exists(NODES_LIST):
        with open(NODES_LIST, 'r') as f: 
            lines = f.readlines()
        with open(NODES_LIST, 'w') as f:
            for line in lines:
                if line.strip() and not line.startswith(f"{node_id}|") and not line.startswith(f"{node_id} "): 
                    f.write(line)
                    
    groups = load_auto_groups()
    is_auto = False
    for gid, gdata in groups.items():
        if node_id in gdata.get("nodes", {}):
            del groups[gid]["nodes"][node_id]
            save_auto_groups(groups)
            is_auto = True
            break
            
    config = load_config()
    if node_id in config.get('disabled_nodes', []): 
        config['disabled_nodes'].remove(node_id)
        save_config(config)
        
    if is_auto: 
        return redirect(request.referrer)
    return redirect(url_for('dashboard'))

@app.route('/replace_id/<current_id>', methods=['POST'])
def replace_id(current_id):
    old_id = request.form.get('old_id', '').strip()
    nodes = get_all_servers()
    if current_id not in nodes or not old_id: 
        return redirect(f'/node/{current_id}')
    
    if os.path.exists(NODES_LIST):
        with open(NODES_LIST, 'r') as f: 
            lines = f.readlines()
        with open(NODES_LIST, 'w') as f:
            for line in lines:
                if line.strip():
                    if line.startswith(f"{current_id}|") or line.startswith(f"{current_id} "):
                        if '|' in line:
                            parts = line.split('|')
                            f.write(f"{old_id}|{parts[1]}|{parts[2]}\n")
                        else:
                            parts = line.rsplit(' ', 1)
                            f.write(f"{old_id} {parts[1]}\n")
                    else: 
                        f.write(line)
                    
    groups = load_auto_groups()
    for gid, gdata in groups.items():
        if current_id in gdata.get("nodes", {}):
            ndata = gdata["nodes"][current_id]
            del groups[gid]["nodes"][current_id]
            groups[gid]["nodes"][old_id] = ndata
            save_auto_groups(groups)
            break
            
    new_ip = get_target_ip(old_id)
    if new_ip:
        with db_lock:
            db = {}
            if os.path.exists(USERS_DB):
                with open(USERS_DB, 'r') as f: db = json.load(f)
            
            cmds_to_sync = []
            db_changed = False
            
            for uname, uinfo in db.items():
                if isinstance(uinfo, dict) and uinfo.get('node') == current_id:
                    uinfo['node'] = old_id
                    uid = uinfo.get('uuid')
                    port = uinfo.get('port')
                    proto = uinfo.get('protocol', 'v2')
                    safe_u = urllib.parse.quote(uname)
                    
                    if proto == 'v2':
                        uinfo['key'] = f"vless://{uid}@{new_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
                        cmd = f"/usr/local/bin/v2ray-node-add-vless {uname} {uid}"
                    else:
                        credentials = f"chacha20-ietf-poly1305:{uid}"
                        b64_creds = base64.urlsafe_b64encode(credentials.encode('utf-8')).decode('utf-8').rstrip('=')
                        uinfo['key'] = f"ss://{b64_creds}@{new_ip}:{port}#{safe_u}"
                        cmd = f"/usr/local/bin/v2ray-node-add-out {uname} {uid} {port} ; ufw allow {port}/tcp >/dev/null 2>&1 || true ; ufw allow {port}/udp >/dev/null 2>&1 || true"
                        
                    db_changed = True
                    if not uinfo.get('is_blocked', False):
                        cmds_to_sync.append(cmd)
                        
            if db_changed:
                with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)
                
        if cmds_to_sync:
            prefix = "systemctl() { true; }; export -f systemctl; "
            suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
            execute_ssh_bg(new_ip, [prefix + " ; ".join(cmds_to_sync) + suffix])
            
    return redirect(f'/node/{old_id}')

@app.route('/api/check_ssh/<node_id>')
def check_ssh(node_id):
    ip = get_target_ip(node_id)
    if not ip: 
        return jsonify({"status": "error", "msg": "IP not found in nodes list."})
    
    try:
        cmd = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{ip} 'echo ok'"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if "ok" in res.stdout: 
            return jsonify({"status": "success"})
        else:
            return jsonify({"status": "error", "msg": res.stderr.strip()})
    except Exception as e: 
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/check_xray/<node_id>')
def check_xray(node_id):
    ip = get_target_ip(node_id)
    if not ip: 
        return jsonify({"status": "inactive"})
        
    try:
        res = subprocess.run(f"ssh -o ConnectTimeout=3 -o StrictHostKeyChecking=no root@{ip} 'systemctl is-active xray'", shell=True, capture_output=True, text=True)
        if "active" in res.stdout.strip().lower(): 
            return jsonify({"status": "active"})
    except: 
        pass
    return jsonify({"status": "inactive"})

@app.route('/api/stats/<node_id>')
def api_stats(node_id):
    ip = get_target_ip(node_id)
    if not ip: 
        return jsonify({"status": "error"})
        
    try:
        res = subprocess.run(f"ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no root@{ip} \"/usr/local/bin/xray api statsquery --server=127.0.0.1:10085\"", shell=True, capture_output=True, text=True)
        stats = json.loads(res.stdout).get("stat", [])
        data = {}
        for s in stats:
            p = s.get("name", "").split(">>>")
            v = s.get("value", 0)
            if len(p) >= 4:
                if p[0] == "user": 
                    data[p[1]] = data.get(p[1], 0) + v
                elif p[0] == "inbound" and p[1].startswith("out-"): 
                    data[p[1][4:]] = data.get(p[1][4:], 0) + v
        return jsonify({"status": "ok", "data": data})
    except: 
        return jsonify({"status": "error"})

@app.route('/install_node/<node_id>', methods=['POST'])
def install_node_action(node_id):
    ip = get_target_ip(node_id)
    if ip: 
        ip_str = str(ip).strip()
        cmd = f"ssh -o StrictHostKeyChecking=no root@{ip_str} 'bash -s' < /root/PanelMaster/install_node.sh"
        subprocess.run(cmd, shell=True)
    return redirect(request.referrer)

@app.route('/restart_xray/<node_id>', methods=['POST'])
def restart_xray_action(node_id):
    ip = get_target_ip(node_id)
    if ip: 
        execute_ssh_bg(ip, ["systemctl restart xray"])
    return redirect(request.referrer)

@app.route('/toggle_node/<node_id>', methods=['POST'])
def toggle_node(node_id):
    config = load_config()
    if 'disabled_nodes' not in config: 
        config['disabled_nodes'] = []
    
    ip = get_target_ip(node_id)
    
    if node_id in config['disabled_nodes']:
        config['disabled_nodes'].remove(node_id)
        if ip: execute_ssh_bg(ip, ["systemctl start xray"])
    else:
        config['disabled_nodes'].append(node_id)
        if ip: execute_ssh_bg(ip, ["systemctl stop xray"])
        
    save_config(config)
    return redirect(request.referrer)

@app.route('/add_user_manual', methods=['POST'])
def add_user_manual():
    nid = request.form.get('node_id')
    nip = get_target_ip(nid)
    if not nip: 
        return redirect(f'/node/{nid}')
    
    gid = ""
    groups = load_auto_groups()
    for g_id, gdata in groups.items():
        if nid in gdata.get("nodes", {}): 
            gid = g_id
            break

    mode = request.form.get('creation_mode', 'single')
    raw_usernames = []
    if mode == 'single': 
        raw_usernames = [request.form.get('single_username', '')]
    elif mode == 'list': 
        raw_usernames = re.split(r'[,\n\r]+', request.form.get('list_usernames', ''))
    elif mode == 'pattern':
        base = request.form.get('base_name', '').strip()
        try: start = int(request.form.get('start_num', 1))
        except: start = 1
        try: qty = int(request.form.get('qty', 1))
        except: qty = 1
        raw_usernames = [f"{base}{start+i}" for i in range(qty)]

    try: gb = float(request.form.get('total_gb') or 0)
    except: gb = 0.0
    try: days = int(request.form.get('expire_days') or 30)
    except: days = 30
    
    proto = request.form.get('protocol', 'v2')
    
    success, msg = add_keys(nid, gid, raw_usernames, gb, days, proto, is_auto=False)
    if not success: 
        return f"<script>alert('{msg}'); window.history.back();</script>"
        
    return redirect(request.referrer)

@app.route('/toggle_user/<username>', methods=['POST'])
def toggle_user(username):
    toggle_key(username)
    return redirect(request.referrer)

@app.route('/switch_user_node/<username>', methods=['POST'])
def switch_user_node(username):
    target_node_raw = request.form.get('target_node', '').strip()
    if not target_node_raw:
        return redirect(request.referrer)

    def _norm(s):
        return str(s or "").strip().lower()

    # Resolve node by id/name in a tolerant way (case-insensitive, [AUTO] name support).
    target_node = None
    raw_n = _norm(target_node_raw)
    for nid, ndata in get_all_servers().items():
        nid_n = _norm(nid)
        name = str(ndata.get('name', '')).strip()
        name_n = _norm(name)
        name_no_auto = name
        if name_no_auto.startswith("[AUTO]"):
            name_no_auto = name_no_auto.replace("[AUTO]", "", 1).strip()
        name_no_auto_n = _norm(name_no_auto)
        if raw_n in {nid_n, name_n, name_no_auto_n}:
            target_node = nid
            break

    if not target_node:
        return redirect(request.referrer)

    with db_lock:
        db = {}
        if not os.path.exists(USERS_DB):
            return redirect(request.referrer)
        with open(USERS_DB, 'r') as f:
            db = json.load(f)

        if username not in db or not isinstance(db.get(username), dict):
            return redirect(request.referrer)

        uinfo = db[username]
        old_node = uinfo.get('node')
        if old_node == target_node:
            return redirect(request.referrer)

        group_id = uinfo.get('group')
        if group_id:
            groups = load_auto_groups()
            g_nodes = groups.get(group_id, {}).get("nodes", {})
            g_nodes_norm = {str(nid).strip().lower(): nid for nid in g_nodes.keys()}
            if target_node not in g_nodes:
                target_node = g_nodes_norm.get(str(target_node).strip().lower(), target_node)
            if target_node not in g_nodes:
                return redirect(request.referrer)

        new_ip = get_target_ip(target_node)
        if not new_ip:
            return redirect(request.referrer)
        new_ip = str(new_ip).strip()

        old_ip = get_target_ip(old_node)
        old_ip = str(old_ip).strip() if old_ip else None

        uid = uinfo.get('uuid')
        port = uinfo.get('port')
        proto = uinfo.get('protocol', 'out')
        safe_u = urllib.parse.quote(username)
        is_blocked = uinfo.get('is_blocked', False)

        # Collect pending bytes from old active node before switching.
        if old_ip:
            try:
                cmd_stats = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{old_ip} '/usr/local/bin/xray api statsquery --server=127.0.0.1:10085'"
                res = subprocess.run(cmd_stats, shell=True, capture_output=True, text=True, timeout=8)
                if res.stdout:
                    stats = json.loads(res.stdout).get("stat", [])
                    current_val = 0.0
                    for s in stats:
                        p = s.get("name", "").split(">>>")
                        if len(p) >= 4 and p[0] == "user" and p[1] == username:
                            current_val += float(s.get("value", 0))

                    last_val = float(uinfo.get('last_raw_bytes', 0.0))
                    if current_val > last_val:
                        uinfo['used_bytes'] = float(uinfo.get('used_bytes', 0)) + (current_val - last_val)
                    elif current_val < last_val and current_val > 0:
                        uinfo['used_bytes'] = float(uinfo.get('used_bytes', 0)) + current_val
            except Exception:
                pass

        uinfo['node'] = target_node
        if proto == 'v2':
            uinfo['key'] = f"vless://{uid}@{new_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
        else:
            b64_creds = base64.urlsafe_b64encode(f"chacha20-ietf-poly1305:{uid}".encode('utf-8')).decode('utf-8').rstrip('=')
            uinfo['key'] = f"ss://{b64_creds}@{new_ip}:{port}#{safe_u}"
        uinfo['last_raw_bytes'] = 0

        with open(USERS_DB, 'w') as f:
            json.dump(db, f, indent=4)

    if not is_blocked:
        groups = load_auto_groups()
        g_nodes = groups.get(group_id, {}).get("nodes", {}) if group_id else {target_node: {}}
        for nid in g_nodes:
            nip = get_target_ip(nid)
            if not nip:
                continue
            nip = str(nip).strip()
            if nip == new_ip:
                if proto == 'v2':
                    cmd_add = f"/usr/local/bin/v2ray-node-add-vless {username} {uid} ; systemctl restart xray"
                    run_ssh_sync(nip, cmd_add)
                else:
                    cmd_add = f"/usr/local/bin/v2ray-node-add-out {username} {uid} {port} ; ufw allow {port}/tcp >/dev/null 2>&1 || true ; ufw allow {port}/udp >/dev/null 2>&1 || true ; systemctl restart xray"
                    run_ssh_sync(nip, cmd_add)
            else:
                cmd_del = get_safe_delete_cmd(username, proto, port if proto != 'v2' else '443')
                if proto == 'v2':
                    cmd_full_del = f"{cmd_del} ; systemctl restart xray"
                else:
                    cmd_full_del = f"{cmd_del} ; ufw delete allow {port}/tcp >/dev/null 2>&1 || true ; ufw delete allow {port}/udp >/dev/null 2>&1 || true ; systemctl restart xray"
                execute_ssh_bg(nip, [cmd_full_del])

    return redirect(request.referrer or url_for('dashboard'))

@app.route('/edit_user/<username>', methods=['POST'])
def edit_user_route(username):
    try: gb = float(request.form.get('total_gb') or 0)
    except: gb = None
    exp = request.form.get('expire_date', '')
    new_uuid = request.form.get('uuid', '').strip()
    
    edit_key(username, gb, exp)
    
    if new_uuid:
        with db_lock:
            db = {}
            if os.path.exists(USERS_DB):
                with open(USERS_DB, 'r') as f: db = json.load(f)
                
            if username in db:
                uinfo = db[username]
                old_uuid = uinfo.get('uuid') or uinfo.get('password')
                
                if old_uuid and old_uuid != new_uuid:
                    if 'uuid' in uinfo: uinfo['uuid'] = new_uuid
                    elif 'password' in uinfo: uinfo['password'] = new_uuid
                    if 'key' in uinfo and old_uuid in uinfo['key']:
                        uinfo['key'] = uinfo['key'].replace(old_uuid, new_uuid)
                    with open(USERS_DB, 'w') as f: json.dump(db, f)
                    
                    node_id = uinfo.get('node')
                    node_ip = get_target_ip(node_id)
                    if node_ip:
                        cmd = f"sed -i 's/{old_uuid}/{new_uuid}/g' /usr/local/etc/xray/config.json && systemctl restart xray"
                        execute_ssh_bg(node_ip, [cmd])

    return redirect(request.referrer)

@app.route('/renew_user/<username>', methods=['POST'])
def renew_user_route(username):
    try: add_gb = float(request.form.get('add_gb') or 50)
    except: add_gb = 50.0
    try: add_days = int(request.form.get('add_days') or 30)
    except: add_days = 30
    renew_key(username, add_gb, add_days)
    return redirect(request.referrer)

@app.route('/delete_user/<username>', methods=['POST'])
def delete_user_route(username):
    delete_key(username)
    return redirect(request.referrer)

@app.route('/bulk_delete', methods=['POST'])
def bulk_delete_route():
    usernames = request.form.getlist('usernames')
    bulk_delete_keys(usernames)
    return redirect(request.referrer)

@app.route('/create_node_backup/<node_id>', methods=['POST'])
def create_node_backup(node_id):
    if os.path.exists(USERS_DB):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"backup_{node_id}_{timestamp}.json"
        node_data = {}
        with db_lock:
            with open(USERS_DB, 'r') as f: 
                db = json.load(f)
            for uname, info in db.items():
                if isinstance(info, dict) and info.get('node') == node_id: 
                    node_data[uname] = info
        if node_data:
            with open(os.path.join(BACKUP_DIR, backup_name), 'w') as f: 
                json.dump(node_data, f, indent=4)
    return redirect(request.referrer)

@app.route('/download_backup/<filename>')
def download_backup(filename):
    path = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(path): 
        return send_file(path, as_attachment=True)
    return redirect(request.referrer)

@app.route('/delete_backup/<filename>', methods=['POST'])
def delete_backup(filename):
    path = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(path): 
        os.remove(path)
    return redirect(request.referrer)

@app.route('/purge_node/<node_id>', methods=['POST'])
def purge_node(node_id):
    with db_lock:
        if os.path.exists(USERS_DB):
            with open(USERS_DB, 'r') as f: 
                db = json.load(f)
            users_to_delete = [u for u, info in db.items() if isinstance(info, dict) and info.get('node') == node_id]
            for u in users_to_delete: 
                del db[u]
            with open(USERS_DB, 'w') as f: 
                json.dump(db, f)
                
    if os.path.exists(BACKUP_DIR):
        for f in os.listdir(BACKUP_DIR):
            if f.startswith(f"backup_{node_id}_"): 
                os.remove(os.path.join(BACKUP_DIR, f))
    return redirect(request.referrer)

@app.route('/download_backup_global')
def download_backup_global():
    if os.path.exists(USERS_DB): 
        return send_file(USERS_DB, as_attachment=True, download_name=f"qito_db_backup.json")
    return "No DB found."

@app.route('/upload_backup', methods=['POST'])
def upload_backup():
    file = request.files.get('backup_file')
    if not file: return redirect(url_for('dashboard'))
    
    try:
        uploaded_data = json.load(file)
        
        with db_lock:
            db = {}
            if os.path.exists(USERS_DB):
                try:
                    with open(USERS_DB, 'r') as f: db = json.load(f)
                except: pass
            
            for uname, uinfo in uploaded_data.items():
                db[uname] = uinfo
            
            cmds_by_ip = {}
            for uname, uinfo in db.items():
                if not isinstance(uinfo, dict): continue
                
                node_id = uinfo.get('node')
                node_ip = get_target_ip(node_id)
                if not node_ip: continue
                
                uid = uinfo.get('uuid')
                port = uinfo.get('port')
                proto = uinfo.get('protocol', 'v2')
                safe_u = urllib.parse.quote(uname)
                
                if proto == 'v2':
                    expected_key = f"vless://{uid}@{node_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
                    cmd = f"/usr/local/bin/v2ray-node-add-vless {uname} {uid}"
                else:
                    credentials = f"chacha20-ietf-poly1305:{uid}"
                    b64_creds = base64.urlsafe_b64encode(credentials.encode('utf-8')).decode('utf-8').rstrip('=')
                    expected_key = f"ss://{b64_creds}@{node_ip}:{port}#{safe_u}"
                    cmd = f"/usr/local/bin/v2ray-node-add-out {uname} {uid} {port} ; ufw allow {port}/tcp >/dev/null 2>&1 || true ; ufw allow {port}/udp >/dev/null 2>&1 || true"
                
                uinfo['key'] = expected_key
                if not uinfo.get('is_blocked', False):
                    cmds_by_ip.setdefault(node_ip, []).append(cmd)
            
            with open(USERS_DB, 'w') as f:
                json.dump(db, f, indent=4)
                
        for ip, cmds in cmds_by_ip.items():
            prefix = "systemctl() { true; }; export -f systemctl; "
            suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
            execute_ssh_bg(ip, [prefix + " ; ".join(cmds) + suffix])
            
    except Exception as e:
        print(f"Restore Error: {e}")
        
    return redirect(url_for('dashboard'))

@app.route('/save_settings_basic', methods=['POST'])
def save_settings_basic():
    config = load_config()
    try: config['interval'] = int(request.form.get('interval', 12))
    except: config['interval'] = 12
    config['bot_token'] = request.form.get('bot_token', '')
    save_config(config)
    return redirect(url_for('dashboard'))

@app.route('/config_action', methods=['POST'])
def config_action():
    config = load_config()
    ctype = request.form.get('type')
    action = request.form.get('action')
    val = request.form.get('val', '').strip()
    target_list = 'admin_ids' if ctype == 'admin' else 'mod_ids'
    
    if action == 'add' and val:
        if val not in config.get(target_list, []):
            config.setdefault(target_list, []).append(val)
    elif action == 'del' and val:
        if val in config.get(target_list, []):
            config[target_list].remove(val)
            
    save_config(config)
    return redirect(url_for('dashboard'))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8888)
