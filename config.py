import os, json

SECRET_KEY = "qito_super_secret_admin_key"
USERS_DB = "/root/qito_master/users_db.json"
NODES_LIST = "/root/qito_master/nodes_list.txt"
CONFIG_FILE = "/root/qito_master/config.json"
ADMIN_PASS = "admin123"

def load_config():
    config = {
        "interval": 12,
        "bot_token": "",
        "admin_ids": [],
        "mod_ids": [],
        "disabled_nodes": [],
        # Modes: "single_active" (original) | "pre_provision" (fast switch)
        "switch_mode": "single_active"
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                loaded = json.load(f)
                config.update(loaded)
                if not isinstance(config.get('admin_ids'), list): config['admin_ids'] = []
                if not isinstance(config.get('mod_ids'), list): config['mod_ids'] = []
                if not isinstance(config.get('disabled_nodes'), list): config['disabled_nodes'] = []
                if config.get('switch_mode') not in ["single_active", "pre_provision"]:
                    config['switch_mode'] = "single_active"
        except: pass
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as f: 
        json.dump(config, f)
