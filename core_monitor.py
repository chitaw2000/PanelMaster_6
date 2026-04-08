import json, os, time, subprocess, threading, requests
from datetime import datetime

from utils import get_all_servers, db_lock, get_display_name
from core_auto import load_auto_groups
from core_engine import get_safe_delete_cmd, execute_ssh_bg

try:
    from config import USERS_DB, NODES_LIST, load_config
except ImportError:
    USERS_DB = "/root/PanelMaster/users_db.json"
    NODES_LIST = "/root/PanelMaster/nodes_list.txt"

_IP_FAIL_CACHE = {}
_IP_FAIL_LOCK = threading.Lock()
_MONITOR_STATUS = {
    "thread_started": False,
    "started_at": 0,
    "last_loop_at": 0,
    "last_error": "",
    "loop_count": 0,
    "last_sync_attempt_at": 0,
    "last_sync_ok_at": 0,
    "last_sync_user": "",
    "last_sync_status": ""
}
_MONITOR_STATUS_LOCK = threading.Lock()


def _set_monitor_status(**kwargs):
    with _MONITOR_STATUS_LOCK:
        _MONITOR_STATUS.update(kwargs)


def get_monitor_status():
    with _MONITOR_STATUS_LOCK:
        return dict(_MONITOR_STATUS)


def _parse_monitor_interval(raw_interval):
    try:
        val = float(raw_interval)
    except Exception:
        return 12.0
    if val < 1.0:
        return 1.0
    return val


def _get_sync_targets():
    cfg = {}
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    primary = str(cfg.get("external_sync_url", "")).strip()
    if not primary:
        primary = str(
            os.environ.get(
                "PANEL_SYNC_PRIMARY_URL",
                "https://dash1.dabazinme.me/api/internal/sync-user-usage"
            )
        ).strip()
    return [primary] if primary else []


def _get_sync_api_key():
    # Prefer panel config value so operator can rotate from dashboard.
    cfg = {}
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    key = str(cfg.get("external_sync_api_key", "")).strip()
    if key:
        return key
    return str(
        os.environ.get(
            "PANEL_SYNC_API_KEY",
            "pmk_XI1fBk3DEEekIDwgngJWQmjFXR0TziWkzw9UvmNB_Uk"
        )
    ).strip()


def _skip_ip_temporarily(ip):
    now = time.time()
    with _IP_FAIL_LOCK:
        rec = _IP_FAIL_CACHE.get(ip, {"fails": 0, "retry_at": 0.0})
        if rec.get("retry_at", 0.0) > now:
            return True
    return False


def _mark_ip_result(ip, ok):
    now = time.time()
    with _IP_FAIL_LOCK:
        if ok:
            _IP_FAIL_CACHE.pop(ip, None)
            return
        rec = _IP_FAIL_CACHE.get(ip, {"fails": 0, "retry_at": 0.0})
        fails = int(rec.get("fails", 0)) + 1
        # Exponential backoff up to 10 minutes for dead/inactive nodes.
        backoff = min(600, 10 * (2 ** min(fails, 6)))
        _IP_FAIL_CACHE[ip] = {"fails": fails, "retry_at": now + backoff}

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
    if not ip:
        return totals
    if _skip_ip_temporarily(ip):
        return totals
    try:
        cmd = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{ip} '/usr/local/bin/xray api statsquery --server=127.0.0.1:10085'"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=8)
        if res.returncode != 0 or not res.stdout:
            _mark_ip_result(ip, False)
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
        _mark_ip_result(ip, True)
    except Exception:
        _mark_ip_result(ip, False)
    return totals

def sync_usage_to_subpanel(db_key, uinfo, node_active_count=0):
    try:
        username = get_display_name(db_key, uinfo)
        now_ts = int(time.time())
        _set_monitor_status(last_sync_attempt_at=now_ts, last_sync_user=str(username))
        used_bytes = float(uinfo.get('used_bytes', 0) or 0)
        total_gb = float(uinfo.get('total_gb', 0) or 0)
        used_gb = used_bytes / (1024 ** 3)
        remaining_gb = max(total_gb - used_gb, 0.0)
        online_ips = uinfo.get('online_on_ips', [])

        payload = {
            "name": username,
            "usedGB": round(used_gb, 4),
            "totalGB": total_gb,
            "remainingGB": round(remaining_gb, 4),
            "expireDate": uinfo.get('expire_date'),
            "isBlocked": bool(uinfo.get('is_blocked', False)),
            "isActive": bool(uinfo.get('is_online', False)) and not bool(uinfo.get('is_blocked', False)),
            "node": uinfo.get('node', ''),
            "group": uinfo.get('group', ''),
            "activeOnIps": online_ips if isinstance(online_ips, list) else [],
            "nodeActiveUsers": int(node_active_count)
        }

        headers = {"Content-Type": "application/json", "x-api-key": _get_sync_api_key()}
        urls = _get_sync_targets()

        delivered = False
        for url in urls:
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=6)
                body_preview = (r.text or "").strip().replace("\n", " ")[:240]
                print(f"[usage-sync] user={username} url={url} status={r.status_code} body={body_preview}")
                if 200 <= r.status_code < 300:
                    delivered = True
                    _set_monitor_status(
                        last_sync_ok_at=now_ts,
                        last_sync_user=str(username),
                        last_sync_status=f"{r.status_code} {url}"
                    )
                    break
            except Exception:
                print(f"[usage-sync] user={username} url={url} error=request_failed")

        if not delivered:
            _set_monitor_status(last_sync_status="failed_all_targets", last_sync_user=str(username))
            print(f"[usage-sync] user={username} result=failed_all_targets")
    except Exception:
        _set_monitor_status(last_sync_status="exception", last_sync_user=str(username))
        print(f"[usage-sync] user={username} result=exception")

def sync_node_stats_to_subpanel(groups, db):
    """Push per-group node active user counts to external panel."""
    try:
        headers = {"Content-Type": "application/json", "x-api-key": _get_sync_api_key()}
        base_urls = _get_sync_targets()
        if not base_urls:
            return

        for gid, gdata in groups.items():
            g_nodes = gdata.get("nodes", {})
            if not g_nodes:
                continue

            node_counts = {}
            for nid in g_nodes:
                nip = str(get_target_ip(nid) or "").strip()
                count = 0
                if nip:
                    for ui in db.values():
                        if not isinstance(ui, dict) or ui.get('is_blocked'):
                            continue
                        if ui.get('group') != gid:
                            continue
                        oips = ui.get('online_on_ips', [])
                        if isinstance(oips, list) and nip in oips:
                            count += 1
                node_counts[nid] = count

            payload = {
                "masterGroupId": gid,
                "nodes": node_counts
            }

            for base_url in base_urls:
                url = base_url.rsplit("/", 1)[0] + "/sync-node-stats"
                try:
                    r = requests.post(url, json=payload, headers=headers, timeout=6)
                    if 200 <= r.status_code < 300:
                        print(f"[node-stats-sync] group={gid} url={url} status={r.status_code} nodes={node_counts}")
                        break
                except Exception:
                    pass
    except Exception as e:
        print(f"[node-stats-sync] error: {e}")


def get_user_monitor_ips(uinfo, groups, monitor_skip_nodes=None):
    ips = []
    group_id = uinfo.get('group')
    target_node = uinfo.get('node')
    skip_set = set()
    if isinstance(monitor_skip_nodes, (list, tuple, set)):
        skip_set = {str(x).strip().lower() for x in monitor_skip_nodes if str(x).strip()}

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
            if str(nid).strip().lower() in skip_set:
                continue
            nip = get_target_ip(nid)
            if nip:
                ips.append(str(nip).strip())
    else:
        # Final fallback: at least monitor active node to keep auto-block working.
        if str(target_node).strip().lower() in skip_set:
            target_node = ""
        nip = get_target_ip(target_node)
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
    _set_monitor_status(thread_started=True, started_at=int(time.time()))
    print("[monitor] traffic monitor thread started")
    while True:
        try:
            config = load_config()
            interval = _parse_monitor_interval(config.get('interval', 12))
            monitor_skip_nodes = config.get('monitor_skip_nodes', [])
        except:
            interval = 12.0
            monitor_skip_nodes = []

        try:
            time.sleep(interval)
        except Exception:
            time.sleep(12.0)
        try:
            _set_monitor_status(last_loop_at=int(time.time()), loop_count=int(get_monitor_status().get("loop_count", 0)) + 1)
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
                ips = get_user_monitor_ips(uinfo, groups, monitor_skip_nodes=monitor_skip_nodes)
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
                        enforced = suspend_user_everywhere(get_display_name(uname, uinfo), uinfo)
                        if enforced:
                            uinfo['block_enforced'] = True
                            db_changed = True
                    continue

                if uname not in user_ips_map:
                    continue

                display = get_display_name(uname, uinfo)

                last_map = uinfo.get('last_raw_bytes_map')
                if not isinstance(last_map, dict):
                    last_map = {}

                total_diff = 0.0
                current_total = 0.0
                active_ips = []

                for ip in user_ips_map[uname]:
                    current_val = float(ip_totals_map.get(ip, {}).get(display, 0.0))
                    last_val = float(last_map.get(ip, 0.0) or 0.0)

                    diff = 0.0
                    if current_val > last_val:
                        diff = current_val - last_val
                    elif current_val < last_val and current_val > 0:
                        diff = current_val

                    if diff > 0:
                        total_diff += diff
                        active_ips.append(ip)

                    last_map[ip] = current_val
                    current_total += current_val

                if total_diff > 0:
                    uinfo['used_bytes'] = float(uinfo.get('used_bytes', 0)) + total_diff
                    db_changed = True

                now_online = total_diff > 0
                if bool(uinfo.get('is_online', False)) != now_online:
                    uinfo['is_online'] = now_online
                    db_changed = True

                new_active_ips = sorted(active_ips) if active_ips else []
                old_active_ips = uinfo.get('online_on_ips', [])
                if new_active_ips != old_active_ips:
                    uinfo['online_on_ips'] = new_active_ips
                    db_changed = True

                if uinfo.get('last_raw_bytes_map') != last_map:
                    uinfo['last_raw_bytes_map'] = last_map
                    db_changed = True

                # Keep legacy aggregate for backward compatibility.
                if float(uinfo.get('last_raw_bytes', 0.0) or 0.0) != current_total:
                    uinfo['last_raw_bytes'] = current_total
                    db_changed = True

                if total_diff > 0:
                    now_ts = int(time.time())
                    last_sync_at = int(uinfo.get('last_usage_sync_at', 0) or 0)
                    last_sync_bytes = float(uinfo.get('last_sync_used_bytes', 0) or 0)
                    current_used = float(uinfo.get('used_bytes', 0) or 0)
                    delta_since_last_sync = max(current_used - last_sync_bytes, 0.0)
                    if (now_ts - last_sync_at) >= 30 or delta_since_last_sync >= (50 * 1024 * 1024):
                        user_node_ip = str(get_target_ip(uinfo.get('node')) or "").strip()
                        nac = 0
                        if user_node_ip:
                            for _u, _ui in db.items():
                                if not isinstance(_ui, dict) or _ui.get('is_blocked'):
                                    continue
                                _oips = _ui.get('online_on_ips', [])
                                if isinstance(_oips, list) and user_node_ip in _oips:
                                    nac += 1
                        sync_usage_to_subpanel(uname, uinfo, node_active_count=nac)
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
                    enforced = suspend_user_everywhere(display, uinfo)
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

            now_ts = int(time.time())
            last_node_sync = int(_monitor_status.get("last_node_stats_sync_at", 0) or 0)
            if (now_ts - last_node_sync) >= 30:
                threading.Thread(target=sync_node_stats_to_subpanel, args=(groups, db), daemon=True).start()
                _monitor_status["last_node_stats_sync_at"] = now_ts

        except Exception as e:
            _set_monitor_status(last_error=str(e)[:300])
            print(f"[monitor] loop error: {e}")

def start_background_monitor():
    t = threading.Thread(target=monitor_traffic, daemon=True)
    t.start()
