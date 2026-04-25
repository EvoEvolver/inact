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
    # Detect which base path flag is supported (varies across code-server/openvscode builds)
    CS_HELP=$(code-server --help 2>&1 || true)
    CS_PATH_MODE="no-base-path"
    BASE_FLAG=()
    if echo "$CS_HELP" | grep -q -- '--base-path'; then
        CS_PATH_MODE="base-path"
        BASE_FLAG=(--base-path /vscode)
    elif echo "$CS_HELP" | grep -q -- '--server-base-path'; then
        CS_PATH_MODE="server-base-path"
        BASE_FLAG=(--server-base-path /vscode)
    fi
    export CS_PATH_MODE

    set -x
    code-server \
        --port          "${CODE_SERVER_PORT}" \
        --auth          none \
        "${BASE_FLAG[@]}" \
        --user-data-dir /tmp/code-server-data \
        "${FILES_DIR}" &
    { set +x; } 2>/dev/null
    CS_PID=$!
    echo "code-server pid=${CS_PID} (mode=${CS_PATH_MODE})"
fi

python3 - << 'PYEOF'
import os

port     = int(os.environ.get('PORT', 5050))
internal = 5051
cs_port  = int(os.environ.get('CODE_SERVER_PORT', 0) or 0)
wc       = int(os.environ.get('NGINX_WORKER_CONNECTIONS', 16384) or 16384)
wp       = os.environ.get('NGINX_WORKER_PROCESSES', 'auto') or 'auto'
cs_mode  = os.environ.get('CS_PATH_MODE', '')

cs_block = ""
if cs_port:
    # No trailing slash on proxy_pass — preserves /vscode/ prefix.
    # If code-server doesn't support a base path flag, we rewrite /vscode/* to /
    # and also proxy common absolute asset paths to code-server to keep the app working.
    if cs_mode in ("base-path", "server-base-path"):
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
    else:
        cs_block = f"""
            # Health check endpoint (return immediately so iframe can detect readiness)
            location = /vscode/healthz {{ return 204; }}
            # Redirect bare /vscode to /vscode/
            location = /vscode {{ return 301 /vscode/; }}
            # Rewrite /vscode/* to / for code-server (no base-path support)
            location ^~ /vscode/ {{
                rewrite ^/vscode(/.*)$ $1 break;
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
            # Also proxy common absolute asset paths used by code-server
            location ^~ /static/ {{
                proxy_pass             http://127.0.0.1:{cs_port};
                proxy_http_version     1.1;
                proxy_set_header       Host                 $host;
                proxy_set_header       X-Forwarded-For      $proxy_add_x_forwarded_for;
                proxy_set_header       X-Forwarded-Proto    $scheme;
                proxy_buffering        off;
            }}
            location ^~ /webview/ {{
                proxy_pass             http://127.0.0.1:{cs_port};
                proxy_http_version     1.1;
                proxy_set_header       Host                 $host;
                proxy_set_header       X-Forwarded-For      $proxy_add_x_forwarded_for;
                proxy_set_header       X-Forwarded-Proto    $scheme;
                proxy_buffering        off;
            }}
            location ^~ /vscode-remote-resource {{
                proxy_pass             http://127.0.0.1:{cs_port};
                proxy_http_version     1.1;
                proxy_set_header       Host                 $host;
                proxy_set_header       X-Forwarded-For      $proxy_add_x_forwarded_for;
                proxy_set_header       X-Forwarded-Proto    $scheme;
                proxy_buffering        off;
            }}
            location = /favicon.ico {{
                proxy_pass             http://127.0.0.1:{cs_port}/favicon.ico;
            }}
            location ~ ^/(manifest\.(?:json|webmanifest)|service-worker\.js)$ {{
                proxy_pass             http://127.0.0.1:{cs_port};
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
