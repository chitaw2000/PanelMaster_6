import json, os, time, subprocess, threading
from datetime import datetime

from utils import get_all_servers, db_lock
from core_auto import load_auto_groups
from core_engine import get_safe_delete_cmd

try:
    from config import USERS_DB, NODES_LIST, load_config
except ImportError:
    USERS_DB = "/root/PanelMaster/users_db.json"
    NODES_LIST = "/root/PanelMaster/nodes_list.txt"

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

def suspend_user_everywhere(username, uinfo):
    port = uinfo.get('port')
    group_id = uinfo.get('group')
    target_node = uinfo.get('node')
    groups = load_auto_groups()
    g_nodes = groups.get(group_id, {}).get("nodes", {}) if group_id else {target_node: {}}
    
    for nid in g_nodes:
        nip = get_target_ip(nid)
        if not nip: continue
        cmd_del = get_safe_delete_cmd(username, 'out', port)
        full_del = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@{nip} 'export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; {cmd_del} ; ufw delete allow {port}/tcp >/dev/null 2>&1 || true ; ufw delete allow {port}/udp >/dev/null 2>&1 || true ; systemctl restart xray'"
        subprocess.Popen(full_del, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def query_ip_user_totals(ip):
    totals = {}
    try:
        cmd = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{ip} '/usr/local/bin/xray api statsquery --server=127.0.0.1:10085'"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=8)
        if not res.stdout:
            return totals

        stats = json.loads(res.stdout).get("stat", [])
        for s in stats:
            p = s.get("name", "").split(">>>")
            val = float(s.get("value", 0) or 0)
            if len(p) >= 4 and p[0] == "user":
                uname = p[1]
                totals[uname] = totals.get(uname, 0.0) + val
            elif len(p) >= 4 and p[0] == "inbound" and str(p[1]).startswith("out-"):
                uname = str(p[1])[4:]
                totals[uname] = totals.get(uname, 0.0) + val
    except Exception:
        pass
    return totals

def get_user_monitor_ips(uinfo, groups, switch_mode):
    ips = []
    group_id = uinfo.get('group')
    if group_id and switch_mode == "pre_provision":
        g_nodes = groups.get(group_id, {}).get("nodes", {})
        for nid in g_nodes:
            nip = get_target_ip(nid)
            if nip:
                ips.append(str(nip).strip())
    else:
        nip = get_target_ip(uinfo.get('node'))
        if nip:
            ips.append(str(nip).strip())
    # Keep unique order
    seen = set()
    out = []
    for ip in ips:
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out

def monitor_traffic():
    while True:
        try:
            config = load_config()
            interval = config.get('interval', 12)
        except:
            interval = 12

        time.sleep(interval)
        try:
            with db_lock:
                if not os.path.exists(USERS_DB): continue
                with open(USERS_DB, 'r') as f: db = json.load(f)

            if not db: continue

            switch_mode = config.get("switch_mode", "single_active")
            groups = load_auto_groups()

            # Build monitored IP list per user.
            # pre_provision: group users => all group nodes
            # single_active: users => active node only
            user_ips_map = {}
            all_ips = set()
            for uname, uinfo in db.items():
                if not isinstance(uinfo, dict) or uinfo.get('is_blocked', False):
                    continue
                ips = get_user_monitor_ips(uinfo, groups, switch_mode)
                if ips:
                    user_ips_map[uname] = ips
                    all_ips.update(ips)

            db_changed = False
            current_date = datetime.now().strftime("%Y-%m-%d")

            # Query each IP once.
            ip_totals_map = {}
            for ip in all_ips:
                ip_totals_map[ip] = query_ip_user_totals(ip)

            # Update each user by summing diffs across monitored nodes.
            for uname, uinfo in db.items():
                if uname not in user_ips_map or not isinstance(uinfo, dict) or uinfo.get('is_blocked', False):
                    continue

                last_map = uinfo.get('last_raw_bytes_map')
                if not isinstance(last_map, dict):
                    last_map = {}

                total_diff = 0.0
                current_total = 0.0

                for ip in user_ips_map[uname]:
                    current_val = float(ip_totals_map.get(ip, {}).get(uname, 0.0))
                    last_val = float(last_map.get(ip, 0.0) or 0.0)

                    diff = 0.0
                    if current_val > last_val:
                        diff = current_val - last_val
                    elif current_val < last_val and current_val > 0:
                        # xray reset/restart case on this node
                        diff = current_val

                    if diff > 0:
                        total_diff += diff

                    last_map[ip] = current_val
                    current_total += current_val

                if total_diff > 0:
                    uinfo['used_bytes'] = float(uinfo.get('used_bytes', 0)) + total_diff
                    db_changed = True

                if uinfo.get('last_raw_bytes_map') != last_map:
                    uinfo['last_raw_bytes_map'] = last_map
                    db_changed = True

                # Keep legacy aggregate for backward compatibility.
                if float(uinfo.get('last_raw_bytes', 0.0) or 0.0) != current_total:
                    uinfo['last_raw_bytes'] = current_total
                    db_changed = True

                limit_bytes = float(uinfo.get('total_gb', 0)) * (1024**3)
                is_over_limit = limit_bytes > 0 and float(uinfo.get('used_bytes', 0)) >= limit_bytes
                is_expired = uinfo.get('expire_date') and current_date > uinfo.get('expire_date')

                if is_over_limit or is_expired:
                    uinfo['is_blocked'] = True
                    db_changed = True
                    threading.Thread(target=suspend_user_everywhere, args=(uname, uinfo), daemon=True).start()

            if db_changed:
                with db_lock:
                    with open(USERS_DB, 'r') as f: current_db = json.load(f)
                    for uname, uinfo in db.items():
                        if uname in current_db:
                            current_db[uname].update(uinfo)
                    with open(USERS_DB, 'w') as f: json.dump(current_db, f, indent=4)
                    
        except Exception as e:
            pass

def start_background_monitor():
    t = threading.Thread(target=monitor_traffic, daemon=True)
    t.start()
