import json, os, uuid, base64, urllib.parse, random, string, threading, requests
from datetime import datetime, timedelta
from utils import db_lock, get_all_servers
from core_auto import find_available_node, load_auto_groups, save_auto_groups
from core_engine import execute_ssh_bg, get_safe_delete_cmd

try:
    from config import USERS_DB, NODES_LIST
except ImportError:
    USERS_DB = "/root/PanelMaster/users_db.json"
    NODES_LIST = "/root/PanelMaster/nodes_list.txt"

def get_robust_ip(node_id):
    nodes = get_all_servers()
    if node_id in nodes and nodes[node_id].get('ip'):
        return str(nodes[node_id]['ip']).strip()
    if os.path.exists(NODES_LIST):
        with open(NODES_LIST, 'r') as f:
            for line in f:
                line = line.strip()
                if line and (line.startswith(f"{node_id}|") or line.startswith(f"{node_id} ")):
                    return line.replace('|', ' ').split()[-1]
    return None

def sanitize_usernames(raw_list):
    return [str(u).strip().replace(" ", "_").replace("\r", "").replace("\n", "") for u in raw_list if u]

def generate_token():
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(32))

# 🚀 Sub-Panel သို့ User Data ပို့မည့် Function (JSON Object ပြင်ဆင်ချက်)
def sync_new_user_to_subpanel(username, group_id, total_gb, expire_date, token, uid, port, proto):
    groups = load_auto_groups()
    gdata = groups.get(group_id, {})
    group_name = gdata.get("name", group_id)
    g_nodes = gdata.get("nodes", {})

    keys_dict = {}
    safe_u = urllib.parse.quote(username)
    
    for nid in g_nodes:
        nip = get_robust_ip(nid)
        if not nip: continue
        
        if proto == 'v2':
            keys_dict[nid] = f"vless://{uid}@{nip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
        else:
            # 🚀 Shadowsocks အတွက် JSON Object အတိုင်း ပို့ပေးမည်
            keys_dict[nid] = {
                "server": str(nip),
                "server_port": int(port),
                "password": str(uid),
                "method": "chacha20-ietf-poly1305",
                "prefix": "\u0016\u0003\u0001\u0005\u00f2\u0001\u0000\u0005\u00ee\u0003\u0003"
            }

    payload = {
        "name": username,
        "groupName": group_name,
        "totalGB": float(total_gb),
        "expireDate": expire_date,
        "keys": keys_dict
    }

    try:
        requests.post(
            "http://167.172.91.222:4000/api/internal/sync-user-api",
            json=payload,
            headers={"Content-Type": "application/json", "x-api-key": "My_Super_Secret_VPN_Key_2026"},
            timeout=10
        )
    except Exception as e:
        print(f"Sync Error: {e}")

def add_keys(node_id, group_id, raw_usernames, gb, days, proto, is_auto=False):
    usernames = sanitize_usernames(raw_usernames)
    if not usernames: return False, "❌ No usernames!"

    db = {}
    with db_lock:
        if os.path.exists(USERS_DB):
            try:
                with open(USERS_DB, 'r') as f: db = json.load(f)
            except: pass

        existing_ids = [int(u.get('key_id', 0)) for u in db.values() if isinstance(u, dict) and str(u.get('key_id', '')).isdigit()]
        next_id = max(existing_ids) + 1 if existing_ids else 1
        exp = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        vless_cmds = {}
        ss_cmds = {}
        max_p_by_node = {} 
        
        for uinfo in db.values():
            if isinstance(uinfo, dict) and uinfo.get('protocol') == 'out':
                nid = uinfo.get('node')
                try: p = int(uinfo.get('port', 10000))
                except: p = 10000
                max_p_by_node[nid] = max(max_p_by_node.get(nid, 10000), p)

        for u in usernames:
            if u in db: continue
            if is_auto:
                target_node, target_ip = find_available_node(group_id, 1, current_db=db)
                if not target_node: return False, "❌ Error: Limit Reached! No space available."
            else:
                target_node = node_id
                target_ip = get_robust_ip(node_id)
                if not target_ip: return False, "❌ Error: Node offline!"

            target_ip = str(target_ip).strip()
            max_p = max_p_by_node.get(target_node, 10000)

            uid = str(uuid.uuid4()).strip()
            safe_u = urllib.parse.quote(u)
            token = generate_token()
            
            if proto == 'v2':
                port = "443"
                k = f"vless://{uid}@{target_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{safe_u}"
                cmd = f"/usr/local/bin/v2ray-node-add-vless {u} {uid}"
                vless_cmds.setdefault(target_ip, []).append(cmd)
            else:
                max_p += 1
                max_p_by_node[target_node] = max_p  
                port = str(max_p)
                credentials = f"chacha20-ietf-poly1305:{uid}"
                b64_creds = base64.urlsafe_b64encode(credentials.encode('utf-8')).decode('utf-8').rstrip('=')
                k = f"ss://{b64_creds}@{target_ip}:{port}#{safe_u}"
                cmd = f"/usr/local/bin/v2ray-node-add-out {u} {uid} {port} ; ufw allow {port}/tcp >/dev/null 2>&1 || true ; ufw allow {port}/udp >/dev/null 2>&1 || true"
                ss_cmds.setdefault(target_ip, []).append(cmd)
            
            db[u] = {
                "node": target_node, "group": group_id, "protocol": proto, "uuid": uid, 
                "port": port, "total_gb": float(gb), "expire_date": exp, 
                "used_bytes": 0, "last_raw_bytes": 0, "is_blocked": False, "is_online": False, 
                "key": k, "key_id": next_id, "token": token
            }
            next_id += 1
            
            if is_auto and group_id:
                threading.Thread(target=sync_new_user_to_subpanel, args=(u, group_id, gb, exp, token, uid, port, proto), daemon=True).start()

        with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)
        
        for ip, cmds in vless_cmds.items():
            cmds.append("systemctl restart xray")
            execute_ssh_bg(ip, cmds)
            
        for ip, cmds in ss_cmds.items():
            prefix = "systemctl() { true; }; export -f systemctl; "
            suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
            combined_cmd = prefix + " ; ".join(cmds) + suffix
            execute_ssh_bg(ip, [combined_cmd])
            
        return True, "Success"

def toggle_key(username):
    with db_lock:
        if os.path.exists(USERS_DB):
            with open(USERS_DB, 'r') as f: db = json.load(f)
            if username in db:
                user = db[username]; user['is_blocked'] = not user.get('is_blocked', False)
                ip = get_robust_ip(user.get('node'))
                if ip:
                    protocol = user.get('protocol', 'v2')
                    if user['is_blocked']: 
                        user['is_online'] = False
                        cmd = get_safe_delete_cmd(username, protocol, user.get('port', '443'))
                    else:
                        uid = user['uuid']
                        cmd = f"/usr/local/bin/v2ray-node-add-vless {username} {uid}" if protocol == 'v2' else f"/usr/local/bin/v2ray-node-add-out {username} {uid} {user['port']}"
                    
                    if protocol == 'v2':
                        execute_ssh_bg(str(ip).strip(), [f"{cmd} ; systemctl restart xray"])
                    else:
                        prefix = "systemctl() { true; }; export -f systemctl; "
                        suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
                        execute_ssh_bg(str(ip).strip(), [prefix + cmd + suffix])
                with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)

def edit_key(username, total_gb, expire_date):
    with db_lock:
        if os.path.exists(USERS_DB):
            with open(USERS_DB, 'r') as f: db = json.load(f)
            if username in db:
                if total_gb is not None: db[username]['total_gb'] = float(total_gb)
                if expire_date: db[username]['expire_date'] = expire_date
                with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)

def renew_key(username, add_gb, add_days):
    with db_lock:
        if os.path.exists(USERS_DB):
            with open(USERS_DB, 'r') as f: db = json.load(f)
            if username in db:
                db[username]['total_gb'] = float(add_gb); db[username]['days'] = int(add_days)
                db[username]['expire_date'] = (datetime.now() + timedelta(days=int(add_days))).strftime("%Y-%m-%d")
                db[username]['used_bytes'] = 0; db[username]['last_raw_bytes'] = 0; db[username]['is_blocked'] = False; db[username]['is_online'] = False
                
                ip = get_robust_ip(db[username].get('node'))
                if ip:
                    uid = db[username]['uuid']
                    protocol = db[username]['protocol']
                    port = db[username]['port']
                    if protocol == 'v2':
                        cmd = f"/usr/local/bin/v2ray-node-add-vless {username} {uid}"
                        execute_ssh_bg(str(ip).strip(), [f"{cmd} ; systemctl restart xray"])
                    else:
                        cmd = f"/usr/local/bin/v2ray-node-add-out {username} {uid} {port}"
                        prefix = "systemctl() { true; }; export -f systemctl; "
                        suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
                        execute_ssh_bg(str(ip).strip(), [prefix + cmd + suffix])
                    
                with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)

def delete_key(username):
    with db_lock:
        if os.path.exists(USERS_DB):
            with open(USERS_DB, 'r') as f: db = json.load(f)
            if username in db:
                info = db[username]
                ip = get_robust_ip(info.get('node'))
                protocol = info.get('protocol', 'v2')
                if ip:
                    cmd = get_safe_delete_cmd(username, protocol, info.get('port', '443'))
                    if protocol == 'v2':
                        execute_ssh_bg(str(ip).strip(), [f"{cmd} ; systemctl restart xray"])
                    else:
                        prefix = "systemctl() { true; }; export -f systemctl; "
                        suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
                        execute_ssh_bg(str(ip).strip(), [prefix + cmd + suffix])
                del db[username]
                with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)

def bulk_delete_keys(usernames):
    with db_lock:
        if os.path.exists(USERS_DB):
            with open(USERS_DB, 'r') as f: db = json.load(f)
            vless_dels = {}
            ss_dels = {}
            for uname in usernames:
                if uname in db:
                    ip = get_robust_ip(db[uname].get('node'))
                    protocol = db[uname].get('protocol', 'v2')
                    if ip:
                        ip = str(ip).strip()
                        cmd = get_safe_delete_cmd(uname, protocol, db[uname].get('port', '443'))
                        if protocol == 'v2':
                            vless_dels.setdefault(ip, []).append(cmd)
                        else:
                            ss_dels.setdefault(ip, []).append(cmd)
                    del db[uname]
            with open(USERS_DB, 'w') as f: json.dump(db, f, indent=4)
            
            for ip, cmds in vless_dels.items():
                cmds.append("systemctl restart xray")
                execute_ssh_bg(ip, cmds)
                
            for ip, cmds in ss_dels.items():
                prefix = "systemctl() { true; }; export -f systemctl; "
                suffix = " ; unset -f systemctl; systemctl reset-failed xray; systemctl restart xray"
                combined_cmd = prefix + " ; ".join(cmds) + suffix
                execute_ssh_bg(ip, [combined_cmd])

def rebalance_auto_node(group_id, new_limit, specific_node=None):
    return True, "Success"
