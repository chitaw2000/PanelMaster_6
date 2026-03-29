import json
import os
import re
from datetime import datetime


def _safe_file_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "").strip())


def _fmt_size(path):
    try:
        size_kb = os.path.getsize(path) / 1024.0
        return f"{size_kb:.1f} KB"
    except Exception:
        return "0.0 KB"


def _fmt_time(path):
    try:
        return datetime.fromtimestamp(os.path.getctime(path)).strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return "-"


def _node_id_from_name(filename):
    # New format: node_backup__<node_id>__<timestamp>.json
    if filename.startswith("node_backup__") and filename.endswith(".json"):
        body = filename[len("node_backup__"):-len(".json")]
        parts = body.split("__")
        if len(parts) >= 2:
            return parts[0]

    # Legacy format: backup_<node_id>_<timestamp>.json
    if filename.startswith("backup_") and filename.endswith(".json"):
        body = filename[len("backup_"):-len(".json")]
        # timestamp part has fixed length YYYYMMDD_HHMMSS = 15
        if len(body) > 16 and body[-16] == "_":
            return body[:-16]
        bits = body.split("_")
        if len(bits) >= 2:
            return "_".join(bits[:-2]) if len(bits) > 2 else bits[0]
    return None


def safe_backup_path(backup_dir, filename):
    base = os.path.basename(str(filename or ""))
    if not base or base in {".", ".."}:
        return None
    path = os.path.join(backup_dir, base)
    if os.path.abspath(os.path.dirname(path)) != os.path.abspath(backup_dir):
        return None
    return path


def create_node_backup_snapshot(backup_dir, node_id, db):
    safe_node = _safe_file_name(node_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"node_backup__{safe_node}__{timestamp}.json"
    path = os.path.join(backup_dir, filename)

    users = {}
    for uname, info in (db or {}).items():
        if isinstance(info, dict) and str(info.get("node", "")).strip() == str(node_id).strip():
            users[uname] = info

    payload = {
        "type": "node_backup",
        "version": 1,
        "node_id": str(node_id),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "users": users
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return filename, len(users)


def create_full_backup_snapshot(backup_dir, payload):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"full_backup__{timestamp}.json"
    path = os.path.join(backup_dir, filename)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return filename


def read_backup_json(path):
    with open(path, "r") as f:
        return json.load(f)


def list_backups(backup_dir):
    node_backups = {}
    full_backups = []

    if not os.path.exists(backup_dir):
        return {"node_backups": node_backups, "full_backups": full_backups}

    for filename in sorted(os.listdir(backup_dir), reverse=True):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(backup_dir, filename)
        if not os.path.isfile(path):
            continue

        meta = {
            "filename": filename,
            "size": _fmt_size(path),
            "time": _fmt_time(path)
        }

        if filename.startswith("full_backup__"):
            full_backups.append(meta)
            continue

        nid = _node_id_from_name(filename)
        if not nid:
            continue
        node_backups.setdefault(nid, []).append(meta)

    return {"node_backups": node_backups, "full_backups": full_backups}
