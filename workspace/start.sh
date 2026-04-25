#!/bin/sh
set -e

CODE_SERVER_PORT=${CODE_SERVER_PORT:-0}
FILES_DIR=${DATA_DIR:-/data}/files
# Allow overriding nginx worker limits for high-connection workloads (SSE/ws)
NGINX_WORKER_CONNECTIONS=${NGINX_WORKER_CONNECTIONS:-16384}
NGINX_WORKER_PROCESSES=${NGINX_WORKER_PROCESSES:-auto}

# Start code-server before gunicorn so it is ready when nginx starts routing
if [ "${CODE_SERVER_PORT}" != "0" ]; then
    echo "Starting code-server on :${CODE_SERVER_PORT} for ${FILES_DIR} ..."
    code-server \
        --port          "${CODE_SERVER_PORT}" \
        --auth          none \
        --base-path     /vscode \
        --user-data-dir /tmp/code-server-data \
        "${FILES_DIR}" &
    CS_PID=$!
    echo "code-server pid=${CS_PID}"
fi

python3 - << 'PYEOF'
import os

port     = int(os.environ.get('PORT', 5050))
internal = 5051
cs_port  = int(os.environ.get('CODE_SERVER_PORT', 0) or 0)
wc       = int(os.environ.get('NGINX_WORKER_CONNECTIONS', 16384) or 16384)
wp       = os.environ.get('NGINX_WORKER_PROCESSES', 'auto') or 'auto'

cs_block = ""
if cs_port:
    # No trailing slash on proxy_pass — preserves /vscode/ prefix.
    # code-server started with --base-path /vscode so it serves at /vscode/*.
    cs_block = f"""
        # Ensure trailing slash path works consistently
        location = /vscode {{ return 301 /vscode/; }}
        # WebSocket + long-poll/SSE friendly proxying
        location ^~ /vscode/ {{
            proxy_pass             http://127.0.0.1:{cs_port};
            proxy_http_version     1.1;
            proxy_set_header       Upgrade              $http_upgrade;
            proxy_set_header       Connection           $connection_upgrade;
            proxy_set_header       Host                 $host;
            proxy_set_header       X-Real-IP            $remote_addr;
            proxy_set_header       X-Forwarded-For      $proxy_add_x_forwarded_for;
            proxy_set_header       X-Forwarded-Proto    $scheme;
            proxy_set_header       X-Forwarded-Host     $host;
            proxy_read_timeout     86400;
            proxy_connect_timeout  30s;
            proxy_send_timeout     86400;
            proxy_buffering        off;
        }}
"""

cfg = f"""
worker_processes {wp};
worker_rlimit_nofile 65535;
events {{ worker_connections {wc}; }}
http {{
    # Proper Upgrade header for WebSocket
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        ''      close;
    }}
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
            proxy_read_timeout  86400;
            proxy_send_timeout  86400;
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
