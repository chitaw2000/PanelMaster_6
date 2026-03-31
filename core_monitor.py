import json, os, time, subprocess, threading, requests
from datetime import datetime

from utils import get_all_servers, db_lock
from core_auto import load_auto_groups
from core_engine import get_safe_delete_cmd, execute_ssh_bg

try:
    from config import USERS_DB, NODES_LIST, MASTER_API_KEY, load_config
except ImportError:
    USERS_DB = "/root/PanelMaster/users_db.json"
    NODES_LIST = "/root/PanelMaster/nodes_list.txt"
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

def resolve_user_node_ids(groups, group_id, target_node):
    node_ids = []
    if group_id:
        node_ids = list((groups.get(group_id, {}) or {}).get("nodes", {}).keys())
    if not node_ids and target_node:
        target_norm = str(target_node).strip().lower()
        for _, gdata in groups.items():
            g_nodes = (gdata or {}).get("nodes", {})
            for nid in g_nodes.keys():
                if str(nid).strip().lower() == target_norm:
                    node_ids = list(g_nodes.keys())
                    break
            if node_ids:
                break
    if not node_ids and target_node:
        node_ids = [target_node]
    return node_ids

def suspend_user_everywhere(username, uinfo):
    port = uinfo.get('port')
    group_id = uinfo.get('group')
    target_node = uinfo.get('node')
    proto = uinfo.get('protocol', 'out')
    groups = load_auto_groups()
    node_ids = resolve_user_node_ids(groups, group_id, target_node)
    if proto != 'v2':
        # SS pre-provision safety: hard delete from all known nodes.
        all_node_ids = list(get_all_servers().keys())
        seen = set(str(n).strip().lower() for n in node_ids)
        for nid in all_node_ids:
            nid_n = str(nid).strip().lower()
            if nid_n not in seen:
                node_ids.append(nid)
                seen.add(nid_n)

    ok_count = 0
    total_targets = 0
    for nid in node_ids:
        nip = get_target_ip(nid)
        if not nip:
            continue
        total_targets += 1
        cmd_del = get_safe_delete_cmd(username, proto, port if proto != 'v2' else '443')
        if proto == 'v2':
            remote_cmd = f"export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; {cmd_del} ; systemctl restart xray"
        else:
            remote_cmd = f"export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; {cmd_del} ; ufw delete allow {port}/tcp >/dev/null 2>&1 || true ; ufw delete allow {port}/udp >/dev/null 2>&1 || true ; systemctl restart xray"
        try:
            # Use argv form (no shell quoting pitfalls) so usernames/commands stay intact.
            res = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no", f"root@{nip}", remote_cmd],
                capture_output=True,
                text=True,
                timeout=25
            )
            if res.returncode == 0:
                ok_count += 1
            else:
                execute_ssh_bg(nip, [remote_cmd])
        except Exception:
            execute_ssh_bg(nip, [remote_cmd])

    # Enforced only when deletion succeeds on every reachable target node.
    return total_targets > 0 and ok_count == total_targets

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
                uname = str(p[1]).strip()
                if uname:
                    totals[uname] = totals.get(uname, 0.0) + val
                    uname_l = uname.lower()
                    if uname_l != uname:
                        totals[uname_l] = totals.get(uname_l, 0.0) + val
            elif len(p) >= 4 and p[0] == "inbound" and str(p[1]).startswith("out-"):
                uname = str(p[1])[4:].strip()
                if uname:
                    totals[uname] = totals.get(uname, 0.0) + val
                    uname_l = uname.lower()
                    if uname_l != uname:
                        totals[uname_l] = totals.get(uname_l, 0.0) + val
    except Exception:
        pass
    return totals


def lookup_user_total(ip_totals, username):
    uname = str(username or "").strip()
    if not uname:
        return 0.0
    if uname in ip_totals:
        return float(ip_totals.get(uname, 0.0) or 0.0)
    uname_l = uname.lower()
    if uname_l in ip_totals:
        return float(ip_totals.get(uname_l, 0.0) or 0.0)
    return 0.0

def sync_usage_to_subpanel(username, uinfo):
    # Best-effort usage sync for external panel.
    try:
        used_bytes = float(uinfo.get('used_bytes', 0) or 0)
        total_gb = float(uinfo.get('total_gb', 0) or 0)
        used_gb = used_bytes / (1024 ** 3)
        remaining_gb = max(total_gb - used_gb, 0.0)

        payload = {
            "name": username,
            "usedGB": round(used_gb, 4),
            "totalGB": total_gb,
            "remainingGB": round(remaining_gb, 4),
            "expireDate": uinfo.get('expire_date'),
            "isBlocked": bool(uinfo.get('is_blocked', False))
        }

        headers = {"Content-Type": "application/json", "x-api-key": MASTER_API_KEY}
        urls = [
            "http://167.172.91.222:4000/api/internal/sync-user-usage",
            "http://167.172.91.222:4000/admin/api/internal/sync-user-usage"
        ]

        delivered = False
        for url in urls:
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=6)
                if 200 <= r.status_code < 300:
                    delivered = True
                    break
            except Exception:
                pass

        if not delivered:
            print(f"Usage Sync Failed for {username}")
    except Exception:
        pass

def get_user_monitor_ips(uinfo, groups):
    ips = []
    group_id = uinfo.get('group')
    target_node = uinfo.get('node')
    proto = uinfo.get('protocol', 'out')

    g_nodes = {}
    if group_id:
        g_nodes = (groups.get(group_id, {}) or {}).get("nodes", {})

        # Fallback for stale/incorrect group id: infer by current node membership.
        if not g_nodes and target_node:
            target_norm = str(target_node).strip().lower()
            for _, gdata in groups.items():
                nodes = (gdata or {}).get("nodes", {})
                for nid in nodes.keys():
                    if str(nid).strip().lower() == target_norm:
                        g_nodes = nodes
                        break
                if g_nodes:
                    break

    if g_nodes:
        for nid in g_nodes:
            nip = get_target_ip(nid)
            if nip:
                ips.append(str(nip).strip())
    else:
        # Final fallback: at least monitor active node to keep auto-block working.
        nip = get_target_ip(target_node)
        if nip:
            ips.append(str(nip).strip())

    # Pre-provision safety:
    # For SS users, include all known nodes to avoid stale group/node mapping
    # causing fresh traffic on switched/repaired nodes to be missed.
    if proto != 'v2':
        for nid in get_all_servers().keys():
            nip = get_target_ip(nid)
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
            interval_raw = config.get('interval', 12)
            interval = float(interval_raw)
        except Exception:
            interval = 12.0
        interval = max(1.0, interval)
        try:
            time.sleep(interval)
        except Exception:
            time.sleep(12)
        try:
            with db_lock:
                if not os.path.exists(USERS_DB): continue
                with open(USERS_DB, 'r') as f: db = json.load(f)

            if not db: continue

            groups = load_auto_groups()

            # Pre-provision mode support:
            # build monitored IP list per user (group users => all group nodes).
            user_ips_map = {}
            all_ips = set()
            for uname, uinfo in db.items():
                if not isinstance(uinfo, dict) or uinfo.get('is_blocked', False):
                    continue
                ips = get_user_monitor_ips(uinfo, groups)
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
                if not isinstance(uinfo, dict):
                    continue

                # If user is already blocked, keep retrying node-side enforcement until success.
                if uinfo.get('is_blocked', False):
                    if not bool(uinfo.get('block_enforced', False)):
                        enforced = suspend_user_everywhere(uname, uinfo)
                        if enforced:
                            uinfo['block_enforced'] = True
                            db_changed = True
                    continue

                if uname not in user_ips_map:
                    continue

                last_map = uinfo.get('last_raw_bytes_map')
                if not isinstance(last_map, dict):
                    last_map = {}

                total_diff = 0.0
                current_total = 0.0

                for ip in user_ips_map[uname]:
                    current_val = lookup_user_total(ip_totals_map.get(ip, {}), uname)
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

                # Online means user transferred data in this monitor interval.
                now_online = total_diff > 0
                if bool(uinfo.get('is_online', False)) != now_online:
                    uinfo['is_online'] = now_online
                    db_changed = True

                if uinfo.get('last_raw_bytes_map') != last_map:
                    uinfo['last_raw_bytes_map'] = last_map
                    db_changed = True

                # Keep legacy aggregate for backward compatibility.
                if float(uinfo.get('last_raw_bytes', 0.0) or 0.0) != current_total:
                    uinfo['last_raw_bytes'] = current_total
                    db_changed = True

                # Throttled usage sync to external panel.
                if total_diff > 0:
                    now_ts = int(time.time())
                    last_sync_at = int(uinfo.get('last_usage_sync_at', 0) or 0)
                    last_sync_bytes = float(uinfo.get('last_sync_used_bytes', 0) or 0)
                    current_used = float(uinfo.get('used_bytes', 0) or 0)
                    delta_since_last_sync = max(current_used - last_sync_bytes, 0.0)
                    if (now_ts - last_sync_at) >= 30 or delta_since_last_sync >= (50 * 1024 * 1024):
                        sync_usage_to_subpanel(uname, uinfo)
                        uinfo['last_usage_sync_at'] = now_ts
                        uinfo['last_sync_used_bytes'] = current_used
                        db_changed = True

                limit_bytes = float(uinfo.get('total_gb', 0)) * (1024**3)
                is_over_limit = limit_bytes > 0 and float(uinfo.get('used_bytes', 0)) >= limit_bytes
                is_expired = uinfo.get('expire_date') and current_date > uinfo.get('expire_date')

                if is_over_limit or is_expired:
                    uinfo['is_blocked'] = True
                    uinfo['is_online'] = False
                    uinfo['block_enforced'] = False
                    db_changed = True
                    enforced = suspend_user_everywhere(uname, uinfo)
                    if enforced:
                        uinfo['block_enforced'] = True
                        db_changed = True

            if db_changed:
                with db_lock:
                    with open(USERS_DB, 'r') as f: current_db = json.load(f)
                    for uname, uinfo in db.items():
                        if uname in current_db:
                            current_db[uname].update(uinfo)
                    with open(USERS_DB, 'w') as f: json.dump(current_db, f, indent=4)
                    
        except Exception as e:
            print(f"[monitor_traffic] loop error: {e}")

def start_background_monitor():
    t = threading.Thread(target=monitor_traffic, daemon=True)
    t.start()
