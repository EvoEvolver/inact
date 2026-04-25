#!/bin/sh
set -e

python3 - << 'PYEOF'
import os

port     = int(os.environ.get('PORT', 5050))
internal = 5051
cs_port  = int(os.environ.get('CODE_SERVER_PORT', 0) or 0)

cs_block = ""
if cs_port:
    # No trailing slash on proxy_pass — preserves /vscode/ prefix.
    # code-server started with --base-path /vscode so it serves at /vscode/*.
    cs_block = f"""
        location /vscode {{
            proxy_pass             http://127.0.0.1:{cs_port};
            proxy_http_version     1.1;
            proxy_set_header       Upgrade    $http_upgrade;
            proxy_set_header       Connection upgrade;
            proxy_set_header       Host       $http_host;
            proxy_set_header       X-Real-IP  $remote_addr;
            proxy_read_timeout     86400;
            proxy_connect_timeout  30s;
        }}
"""

cfg = f"""events {{ worker_connections 4096; }}
http {{
    include /etc/nginx/mime.types;
    server {{
        listen {port};
        client_max_body_size 100M;
{cs_block}
        location / {{
            proxy_pass         http://127.0.0.1:{internal};
            proxy_http_version 1.1;
            proxy_set_header   Host              $host;
            proxy_set_header   X-Real-IP         $remote_addr;
            proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header   X-Forwarded-Proto $scheme;
        }}
    }}
}}
"""
with open('/tmp/nginx.conf', 'w') as f:
    f.write(cfg)
print(f"nginx: :{port} -> inact:{internal}" + (f"  /vscode -> code-server:{cs_port}" if cs_port else ""))
PYEOF

nginx -c /tmp/nginx.conf -g "daemon off;" &

exec gunicorn server:wsgi \
    --bind "0.0.0.0:5051" \
    --workers 1 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
