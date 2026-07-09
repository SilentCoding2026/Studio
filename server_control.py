import sys
import subprocess
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
AUTO_STOP_SECS = int(sys.argv[2]) if len(sys.argv) > 2 else 14400
MC_COMMAND = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "java -Xmx2G -jar server.jar nogui"

mc_process = None
shutdown_timer = None
start_time = None
httpd = None
stop_event = threading.Event()  # used to break the serve_forever loop

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
    <title>MC Controller</title>
    <style>
        body {{ font-family: sans-serif; text-align: center; margin-top: 50px; background: #1e1e2f; color: #eee; }}
        button {{ font-size: 18px; padding: 10px 20px; margin: 10px; cursor: pointer; border-radius: 5px; border: none; font-weight: bold; }}
        .start {{ background: #28a745; color: white; }}
        .stop {{ background: #dc3545; color: white; }}
        .restart {{ background: #ffc107; color: black; }}
        .status {{ font-weight: bold; color: #17a2b8; }}
        .uptime {{ color: #aaa; }}
        .cmd {{ font-family: monospace; background: #2d2d44; padding: 8px; color: #0f0; display: inline-block; border-radius: 4px; }}
        .container {{ max-width: 600px; margin: 0 auto; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🎮 Server Control</h1>
        <p>Status: <span class="status">{status}</span></p>
        <p><span class="uptime">Uptime: {uptime} | Remaining: {time_left}</span></p>
        <p>Command: <span class="cmd">{cmd}</span></p>
        <hr>
        <button class="start" onclick="location.href='/start'">▶ Start</button>
        <button class="stop" onclick="location.href='/stop'">⏹ Stop</button>
        <button class="restart" onclick="location.href='/restart'">⟳ Restart</button>
    </div>
</body>
</html>
"""

def auto_stop_trigger():
    print(f"[!] Auto-shutdown limit ({AUTO_STOP_SECS}s) reached. Stopping server...")
    manage_server("stop")

def manage_server(action):
    global mc_process, shutdown_timer, start_time, httpd, stop_event

    is_running = mc_process and mc_process.poll() is None

    if action == "start" and not is_running:
        print("[+] Launching Minecraft server...")
        start_time = time.time()

        # Run without capturing stdout/stderr to avoid deadlocks.
        # Output will appear directly in the GitHub Actions log.
        mc_process = subprocess.Popen(
            MC_COMMAND,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=None,
            stderr=None,
            text=True
        )

        # Arm the auto-shutdown timer
        shutdown_timer = threading.Timer(AUTO_STOP_SECS, auto_stop_trigger)
        shutdown_timer.daemon = True
        shutdown_timer.start()

    elif action == "stop" and is_running:
        if shutdown_timer:
            shutdown_timer.cancel()
            shutdown_timer = None

        print("[+] Stopping server gracefully...")
        try:
            mc_process.stdin.write("stop\n")
            mc_process.stdin.flush()
        except Exception as e:
            print(f"[!] Error sending stop: {e}")

        # Wait up to 30 seconds for it to exit
        try:
            mc_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print("[!] Server didn't stop gracefully, killing it.")
            mc_process.kill()
            mc_process.wait()

        mc_process = None
        start_time = None

        # Signal the web server to shut down (so the GitHub job can finish)
        if httpd and not stop_event.is_set():
            print("[+] Shutting down control panel...")
            stop_event.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global mc_process, start_time

        if self.path in ["/start", "/stop", "/restart"]:
            action = self.path[1:]
            if action == "restart":
                manage_server("stop")
                time.sleep(2)
                manage_server("start")
            else:
                manage_server(action)

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        is_running = mc_process and mc_process.poll() is None
        status = "🟢 RUNNING" if is_running else "🔴 STOPPED"

        uptime_str = "N/A"
        time_left_str = "N/A"
        if is_running and start_time:
            elapsed = int(time.time() - start_time)
            remaining = max(0, int(AUTO_STOP_SECS - elapsed))

            # Uptime
            u_mins, u_secs = divmod(elapsed, 60)
            u_hrs, u_mins = divmod(u_mins, 60)
            uptime_str = f"{u_hrs:02d}:{u_mins:02d}:{u_secs:02d}"

            # Remaining
            r_mins, r_secs = divmod(remaining, 60)
            r_hrs, r_mins = divmod(r_mins, 60)
            time_left_str = f"{r_hrs:02d}:{r_mins:02d}:{r_secs:02d}"

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(
            HTML_PAGE.format(
                status=status,
                uptime=uptime_str,
                time_left=time_left_str,
                cmd=MC_COMMAND
            ).encode()
        )

    def log_message(self, format, *args):
        return  # suppress noisy HTTP logs

if __name__ == "__main__":
    print(f"✅ Minecraft Controller starting on port {PORT}")
    print(f"⏱ Auto-stop in {AUTO_STOP_SECS} seconds ({(AUTO_STOP_SECS/3600):.1f} hours)")

    httpd = HTTPServer(("", PORT), SimpleHandler)
    httpd.allow_reuse_address = True

    # Auto-start the server immediately when the script runs
    manage_server("start")

    # This runs until httpd.shutdown() is called (by the "stop" action)
    while not stop_event.is_set():
        httpd.handle_request()  # Handle one request at a time, checking stop_event

    print("[+] Controller exiting.")