import subprocess
import threading
import base64

def _ssh_task(ip, script_content):
    try:
        b64 = base64.b64encode(script_content.encode('utf-8')).decode('utf-8')
        full_cmd = f"ssh -o ConnectTimeout=20 -o StrictHostKeyChecking=no root@{ip} \"echo {b64} | base64 -d > /tmp/pm_task.sh && bash /tmp/pm_task.sh\""
        subprocess.run(full_cmd, shell=True)
    except Exception:
        pass

def execute_ssh_bg(ip, cmds):
    if not cmds: return
    if isinstance(cmds, list):
        script_content = "\n".join(cmds)
    else:
        script_content = cmds
    threading.Thread(target=_ssh_task, args=(ip, script_content), daemon=True).start()

# 🚀 နာမည်မပြောင်းဘဲ Protocol ပေါ်မူတည်၍ VLESS နှင့် SS အား သီးခြား အလုပ်လုပ်စေမည်
def get_safe_delete_cmd(username, protocol, port):
    if protocol == 'v2':
        return f"yes | /usr/local/bin/v2ray-node-del-vless '{username}' >/dev/null 2>&1 || true"
    else:
        # 🚀 Outline SS အတွက် သီးသန့် Zombie Port ရှင်းလင်းရေး
        py_clean = f"python3 -c \"import json; p='/usr/local/etc/xray/config.json'; d=json.load(open(p)); d['inbounds']=[i for i in d.get('inbounds',[]) if str(i.get('port',''))!='{port}']; json.dump(d,open(p,'w'),indent=4)\""
        return f"{py_clean} ; yes | /usr/local/bin/v2ray-node-del-out '{username}' {port} >/dev/null 2>&1 || true ; ufw delete allow {port}/tcp >/dev/null 2>&1 || true ; ufw delete allow {port}/udp >/dev/null 2>&1 || true"
