import json, os
from utils import db_lock, AUTO_GROUPS_FILE

try:
    from config import USERS_DB
except ImportError:
    USERS_DB = "/root/PanelMaster/users_db.json"

def load_auto_groups():
    if not os.path.exists(AUTO_GROUPS_FILE): return {}
    try:
        with open(AUTO_GROUPS_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_auto_groups(data):
    with open(AUTO_GROUPS_FILE, 'w') as f: json.dump(data, f, indent=4)

# 🚀 Migration လုပ်ရာတွင် Database အခြေအနေကို ချက်ချင်းသိနိုင်ရန် current_db ထည့်သွင်းထားသည်
def find_available_node(group_id, required_qty, current_db=None):
    groups = load_auto_groups()
    if group_id not in groups: return None, None
    group = groups[group_id]
    nodes = group.get("nodes", {})
    if not nodes: return None, None

    if current_db is not None:
        db = current_db
    else:
        with db_lock:
            if os.path.exists(USERS_DB):
                with open(USERS_DB, 'r') as f: db = json.load(f)
            else: db = {}

    counts = {nid: 0 for nid in nodes.keys()}
    for uname, uinfo in db.items():
        nid = uinfo.get("node")
        if nid in counts: counts[nid] += 1

    for nid in sorted(nodes.keys()):
        ndata = nodes[nid]
        if isinstance(ndata, dict):
            limit = int(ndata.get("limit", group.get("limit", 30)))
            nip = str(ndata.get("ip")).strip()
        else:
            limit = int(group.get("limit", 30))
            nip = str(ndata).strip()
            
        if counts[nid] + required_qty <= limit:
            return nid, nip
            
    return None, None
