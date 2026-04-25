#!/usr/bin/env bash
set -e

CODE_SERVER_PORT=${CODE_SERVER_PORT:-0}
FILES_DIR=${DATA_DIR:-/data}/files
# Allow overriding nginx worker limits for high-connection workloads (SSE/ws)
NGINX_WORKER_CONNECTIONS=${NGINX_WORKER_CONNECTIONS:-16384}
NGINX_WORKER_PROCESSES=${NGINX_WORKER_PROCESSES:-auto}

# Start code-server before gunicorn so it is ready when nginx starts routing
if [ "${CODE_SERVER_PORT}" != "0" ]; then
    # Avoid port conflicts with the public PORT and any existing listener
    PUBLIC_PORT=${PORT:-5050}
    pick_free_port() {
        python3 - "$@" <<'PY'
import os, socket, sys
start = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
avoid = set(int(x) for x in sys.argv[2].split(',') if x) if len(sys.argv) > 2 else set()
for p in list(range(start, start + 200)) + [0]:
    if p in avoid: continue
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", p))
        print(s.getsockname()[1])
        sys.exit(0)
    except OSError:
        pass
    finally:
        try: s.close()
        except Exception: pass
print(start)
PY
    }

    # If CODE_SERVER_PORT collides with PUBLIC_PORT or is already in use, pick another
    if [ "${CODE_SERVER_PORT}" = "${PUBLIC_PORT}" ]; then
        NEW_PORT=$(pick_free_port 8081 "${PUBLIC_PORT}")
        echo "CODE_SERVER_PORT=${CODE_SERVER_PORT} conflicts with PORT=${PUBLIC_PORT}; switching to ${NEW_PORT}"
        CODE_SERVER_PORT=${NEW_PORT}
        export CODE_SERVER_PORT
    else
        # Probe usage; if in use, switch
        INUSE=$(python3 - "$CODE_SERVER_PORT" <<'PY'
import socket, sys
p = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", p))
except OSError:
    print("IN_USE")
else:
    print("FREE")
finally:
    try: s.close()
    except Exception: pass
PY
        )
        if [ "$INUSE" = "IN_USE" ]; then
            NEW_PORT=$(pick_free_port 8081 "${PUBLIC_PORT},${CODE_SERVER_PORT}")
            echo "CODE_SERVER_PORT=${CODE_SERVER_PORT} is already in use; switching to ${NEW_PORT}"
            CODE_SERVER_PORT=${NEW_PORT}
            export CODE_SERVER_PORT
        fi
    fi

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
        --host          127.0.0.1 \
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
dav_port = 8083  # WebDAV always on this internal port
wc       = int(os.environ.get('NGINX_WORKER_CONNECTIONS', 16384) or 16384)
wp       = os.environ.get('NGINX_WORKER_PROCESSES', 'auto') or 'auto'
cs_mode  = os.environ.get('CS_PATH_MODE', '')

cs_block = ""
if cs_port:
    # No trailing slash on proxy_pass — preserves /vscode/ prefix.
    # If code-server doesn't support a base path flag, we rewrite /vscode/* to /
    # and also proxy common absolute asset paths to code-server to keep the app working.
    if cs_mode in ("base-path", "server-base-path"):
        cs_block = fr"""
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
        cs_block = fr"""
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
        # WebDAV — agents can mount this as a local filesystem
        location /files/dav/ {{
            proxy_pass         http://127.0.0.1:{dav_port}/;
            proxy_http_version 1.1;
            proxy_set_header   Host              $host;
            proxy_set_header   X-Real-IP         $remote_addr;
            proxy_set_header   Destination       $http_destination;
            proxy_set_header   Overwrite         $http_overwrite;
            proxy_request_buffering off;
            proxy_buffering         off;
            client_max_body_size    0;
        }}

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

# ── WebDAV filesystem server ──────────────────────────────────────────────────
# Serves FILES_DIR over WebDAV so agents can mount it as a local filesystem.
# Always enabled; internal port 8083 (not exposed externally — nginx proxies it).
DAV_PORT=8083
echo "Starting WebDAV for ${FILES_DIR} on :${DAV_PORT} ..."
rclone serve webdav "${FILES_DIR}" \
    --addr "127.0.0.1:${DAV_PORT}" \
    --vfs-cache-mode writes \
    --no-modtime \
    &
echo "WebDAV pid=$!"

nginx -c /tmp/nginx.conf -g "daemon off;" &

exec gunicorn server:wsgi \
    --bind "0.0.0.0:5051" \
    --workers 1 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
