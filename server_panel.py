import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────
PANEL_PORT = int(os.environ.get('PANEL_PORT', '8085'))
MC_HOST = os.environ.get('MC_HOST', 'localhost')
MC_PORT = int(os.environ.get('MC_PORT', '25565'))
SHUTDOWN_HOURS = float(os.environ.get('SHUTDOWN_HOURS', '5.5'))
SERVER_DIR = Path(os.environ.get('SERVER_DIR', 'server'))
MC_LOG = SERVER_DIR / 'mc.log'
PANEL_LOG = Path('panel.log')

# ── State ──────────────────────────────────────────────────────
mc_pid = None
start_time = datetime.now()
deadline = start_time + timedelta(hours=SHUTDOWN_HOURS)
stop_requested = False
stop_reason = ''
shutdown_complete = False


def log(msg: str) -> None:
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{timestamp}] {msg}'
    print(line, flush=True)
    with open(PANEL_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def find_mc_pid() -> int | None:
    """Find the Minecraft server Java process PID."""
    try:
        if sys.platform == 'win32':
            result = subprocess.run(
                ['wmic', 'process', 'where', 'name="java.exe"', 'get', 'ProcessId,CommandLine'],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                if 'minecraft' in line.lower() or 'server' in line.lower():
                    parts = line.strip().split()
                    if parts:
                        return int(parts[-1])
        else:
            result = subprocess.run(
                ['pgrep', '-f', 'java.*minecraft'],
                capture_output=True, text=True, timeout=10
            )
            pids = result.stdout.strip().split()
            if pids:
                return int(pids[0])
    except Exception as e:
        log(f'Error finding MC PID: {e}')

    # fallback: read from pid file
    pid_file = Path('mc.pid')
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except Exception:
            pass
    return None


def send_rcon(command: str) -> bool:
    """Send command to Minecraft server via RCON."""
    rcon_port = int(os.environ.get('RCON_PORT', '25575'))
    rcon_password = os.environ.get('RCON_PASSWORD', '')

    if not rcon_password:
        log('No RCON_PASSWORD set — using subprocess stdin fallback')
        return _send_via_stdin(command)

    try:
        import socket as sock_module
        s = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
        s.settimeout(5)
        s.connect((MC_HOST, rcon_port))

        # RCON packet format: https://wiki.vg/RCON
        request_id = 42
        packet_type = 2  # command
        payload = command.encode('utf-8')
        packet = (
            request_id.to_bytes(4, 'little')
            + packet_type.to_bytes(4, 'little')
            + payload
            + b'\x00\x00'
        )
        length = len(packet).to_bytes(4, 'little')
        s.send(length + packet)
        s.close()
        log(f'RCON sent: {command}')
        return True
    except Exception as e:
        log(f'RCON failed: {e}')
        return _send_via_stdin(command)


def _send_via_stdin(command: str) -> bool:
    """Fallback: send command via mc.pid subprocess stdin."""
    global mc_pid
    if mc_pid is None:
        mc_pid = find_mc_pid()

    if mc_pid is None:
        log('Cannot send command — no MC PID found')
        return False

    try:
        if sys.platform == 'win32':
            log('stdin fallback not supported on Windows')
            return False
        # Write to /proc/<pid>/fd/0 on Linux
        stdin_path = Path(f'/proc/{mc_pid}/fd/0')
        if stdin_path.exists():
            stdin_path.write_text(f'{command}\n')
            log(f'stdin sent: {command}')
            return True
    except Exception as e:
        log(f'stdin fallback failed: {e}')
    return False


def wait_for_server_stop(timeout: int = 120) -> bool:
    """Wait for Minecraft server to stop listening on its port."""
    log('Waiting for Minecraft server to stop...')
    for i in range(timeout):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((MC_HOST, MC_PORT))
            s.close()
            # still listening
            time.sleep(1)
        except (ConnectionRefusedError, OSError):
            log(f'Server stopped after ~{i}s')
            return True
    log('Timeout waiting for server to stop')
    return False


def stop_minecraft(reason: str) -> None:
    """Gracefully stop the Minecraft server."""
    global stop_requested, stop_reason, shutdown_complete
    if stop_requested:
        return
    stop_requested = True
    stop_reason = reason
    log(f'Stopping Minecraft server: {reason}')

    send_rcon('stop')
    send_rcon('save-all')

    stopped = wait_for_server_stop(timeout=120)
    if not stopped:
        log('Force killing Minecraft process...')
        pid = find_mc_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(5)
                os.kill(pid, signal.SIGKILL)
            except Exception as e:
                log(f'Kill error: {e}')

    shutdown_complete = True
    log('Shutdown sequence complete — workflow can proceed')


def shutdown_timer_thread() -> None:
    """Background thread: auto-shutdown when deadline reached."""
    global stop_requested
    while not stop_requested:
        remaining = deadline - datetime.now()
        if remaining.total_seconds() <= 0:
            stop_minecraft(f'Auto-shutdown after {SHUTDOWN_HOURS}h timer expired')
            break
        # check every 30 seconds
        time.sleep(30)


# ── HTTP Handlers ──────────────────────────────────────────────

class PanelHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log(f'HTTP: {args}')

    def _send_response(self, status: int, content_type: str, body: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_page()
        elif self.path == '/api/status':
            self._api_status()
        elif self.path == '/api/stop':
            self._api_stop()
        elif self.path == '/health':
            self._send_response(200, 'text/plain', 'ok')
        else:
            self._send_response(404, 'text/plain', 'Not found')

    def do_POST(self):
        if self.path == '/api/stop':
            self._api_stop()
        else:
            self._send_response(404, 'text/plain', 'Not found')

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _serve_page(self):
        remaining = max(0, (deadline - datetime.now()).total_seconds())
        h, m = divmod(int(remaining), 3600)
        m, s = divmod(m, 60)

        page = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Minecraft Server Panel</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }}
  .panel {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 40px;
    text-align: center;
    max-width: 420px;
    width: 90%;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  }}
  h1 {{
    font-size: 1.5rem;
    margin-bottom: 8px;
    color: #58a6ff;
  }}
  .status {{
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 6px;
  }}
  .status.online {{ background: #3fb950; box-shadow: 0 0 8px #3fb950; }}
  .status.stopping {{ background: #d29922; box-shadow: 0 0 8px #d29922; }}
  .status.offline {{ background: #f85149; box-shadow: 0 0 8px #f85149; }}
  .info {{
    background: #0d1117;
    border-radius: 8px;
    padding: 20px;
    margin: 24px 0;
    text-align: left;
    font-size: 0.9rem;
    line-height: 1.8;
  }}
  .info span {{ color: #8b949e; }}
  .info strong {{ color: #58a6ff; }}
  .timer {{
    font-size: 2rem;
    font-weight: 700;
    color: #58a6ff;
    margin: 12px 0;
  }}
  button {{
    background: #da3633;
    color: #fff;
    border: 1px solid #f85149;
    padding: 14px 40px;
    font-size: 1.1rem;
    font-weight: 600;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.2s;
    width: 100%;
  }}
  button:hover {{ background: #f85149; }}
  button:disabled {{
    background: #30363d;
    border-color: #30363d;
    color: #8b949e;
    cursor: not-allowed;
  }}
  .msg {{
    margin-top: 16px;
    padding: 10px;
    border-radius: 6px;
    font-size: 0.85rem;
  }}
  .msg.warn {{ background: #bb800926; color: #d29922; border: 1px solid #bb8009; }}
  .msg.ok {{ background: #23863626; color: #3fb950; border: 1px solid #238636; }}
</style>
</head>
<body>
<div class="panel">
  <h1><span class="status online" id="status-dot"></span> Minecraft Server</h1>
  <p style="color:#8b949e;font-size:0.85rem;">Control Panel</p>

  <div class="info">
    <div><span>Timer:</span> <strong>{SHUTDOWN_HOURS}h auto-shutdown</strong></div>
    <div><span>Remaining:</span></div>
    <div class="timer" id="timer">{h:02d}:{m:02d}:{s:02d}</div>
    <div><span>Started:</span> <strong>{start_time.strftime('%H:%M:%S')}</strong></div>
    <div><span>Deadline:</span> <strong>{deadline.strftime('%H:%M:%S')}</strong></div>
  </div>

  <button id="stop-btn" onclick="stopServer()">⏹ STOP SERVER</button>
  <div id="msg"></div>
</div>

<script>
  const deadline = new Date("{deadline.isoformat()}").getTime();
  let stopped = false;

  function updateTimer() {{
    if (stopped) return;
    const now = Date.now();
    const remaining = Math.max(0, Math.floor((deadline - now) / 1000));
    const h = Math.floor(remaining / 3600);
    const m = Math.floor((remaining % 3600) / 60);
    const s = remaining % 60;
    document.getElementById('timer').textContent =
      String(h).padStart(2,'0') + ':' +
      String(m).padStart(2,'0') + ':' +
      String(s).padStart(2,'0');
    if (remaining < 600) {{
      document.getElementById('timer').style.color = '#f85149';
    }}
    if (remaining <= 0 && !stopped) {{
      document.getElementById('status-dot').className = 'status stopping';
      document.getElementById('msg').innerHTML =
        '<div class="msg warn">⏳ Auto-shutdown in progress...</div>';
      stopped = true;
    }}
  }}
  setInterval(updateTimer, 1000);

  async function stopServer() {{
    const btn = document.getElementById('stop-btn');
    btn.disabled = true;
    btn.textContent = '⏳ STOPPING...';
    document.getElementById('status-dot').className = 'status stopping';
    document.getElementById('msg').innerHTML =
      '<div class="msg warn">Stopping server — please wait...</div>';

    try {{
      const resp = await fetch('/api/stop', {{ method: 'POST' }});
      const data = await resp.json();
      if (data.ok) {{
        stopped = true;
        document.getElementById('status-dot').className = 'status offline';
        document.getElementById('msg').innerHTML =
          '<div class="msg ok">✅ Server stopped. Workflow will upload world and exit.</div>';
        btn.textContent = '✅ STOPPED';
      }} else {{
        document.getElementById('msg').innerHTML =
          '<div class="msg warn">⚠️ ' + data.message + '</div>';
        btn.disabled = false;
        btn.textContent = '⏹ STOP SERVER';
        document.getElementById('status-dot').className = 'status online';
      }}
    }} catch(e) {{
      document.getElementById('msg').innerHTML =
        '<div class="msg warn">⚠️ Request failed. Server may still be stopping.</div>';
    }}
  }}

  // auto-refresh status every 10s
  setInterval(async () => {{
    try {{
      const r = await fetch('/api/status');
      const d = await r.json();
      if (d.shutdown_complete) {{
        document.getElementById('status-dot').className = 'status offline';
        document.getElementById('stop-btn').disabled = true;
        document.getElementById('stop-btn').textContent = '✅ STOPPED';
        document.getElementById('msg').innerHTML =
          '<div class="msg ok">✅ Server stopped — workflow exiting.</div>';
        stopped = true;
      }}
    }} catch(e) {{}}
  }}, 10000);
</script>
</body>
</html>'''
        self._send_response(200, 'text/html', page)

    def _api_status(self):
        remaining = max(0, (deadline - datetime.now()).total_seconds())
        mc_running = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect((MC_HOST, MC_PORT))
            s.close()
            mc_running = True
        except Exception:
            pass

        self._send_response(200, 'application/json', json.dumps({
            'running': mc_running and not shutdown_complete,
            'remaining_seconds': round(remaining, 1),
            'stop_requested': stop_requested,
            'shutdown_complete': shutdown_complete,
            'stop_reason': stop_reason,
            'deadline': deadline.isoformat(),
            'start_time': start_time.isoformat(),
        }))

    def _api_stop(self):
        if shutdown_complete:
            self._send_response(200, 'application/json',
                json.dumps({'ok': True, 'message': 'Already stopped'}))
            return

        # Run stop in thread so HTTP response isn't blocked
        t = threading.Thread(target=stop_minecraft, args=('User pressed stop button',))
        t.start()

        self._send_response(200, 'application/json',
            json.dumps({'ok': True, 'message': 'Stop command sent'}))


def main():
    log(f'Panel starting on port {PANEL_PORT}')
    log(f'Auto-shutdown in {SHUTDOWN_HOURS}h (deadline: {deadline.isoformat()})')

    # Start auto-shutdown timer
    timer_thread = threading.Thread(target=shutdown_timer_thread, daemon=True)
    timer_thread.start()

    # Start HTTP server
    server = http.server.HTTPServer(('0.0.0.0', PANEL_PORT), PanelHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    # Exit cleanly after shutdown
    log('Panel exiting')
    sys.exit(0)


if __name__ == '__main__':
    main()
