import http.server
import socketserver
import urllib.parse
import urllib.request
import os
import threading
import json

PORT = 8082
CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "server_file_name": "server.jar",
    "server_args": "-Xmx4G -Xms4G",
    "java_version": "25"
}

HTML_FORM = """<!DOCTYPE html>
<html>
<head>
    <title>VPS Remote Control Panel</title>
    <style>
        body {{ font-family: Arial, sans-serif; max-width: 700px; margin: 40px auto; padding: 20px; line-height: 1.6; background-color: #f4f6f9; color: #333; }}
        .card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); margin-bottom: 25px; border: 1px solid #e1e4e8; }}
        .form-group {{ margin-bottom: 15px; }}
        label {{ display: block; font-weight: bold; margin-bottom: 5px; color: #444; }}
        input[type="text"], textarea {{ width: 100%; padding: 10px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; font-family: inherit; }}
        textarea {{ resize: vertical; }}
        .btn {{ color: white; padding: 10px 20px; border: none; cursor: pointer; font-size: 16px; text-decoration: none; display: inline-block; border-radius: 4px; font-weight: bold; }}
        .btn-primary {{ background-color: #007BFF; }}
        .btn-primary:hover {{ background-color: #0056b3; }}
        .btn-success {{ background-color: #28a745; }}
        .btn-success:hover {{ background-color: #218838; }}
        .btn-danger {{ background-color: #DC3545; }}
        .btn-danger:hover {{ background-color: #bd2130; }}
        .message {{ padding: 15px; margin-bottom: 20px; border-radius: 4px; white-space: pre-line; font-family: monospace; }}
        .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
        .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
        .actions {{ margin-top: 20px; display: flex; gap: 10px; justify-content: flex-end; }}
    </style>
</head>
<body>
    <h2>VPS Remote Control Panel</h2>
    <p>Manage file transfers and operational configurations directly on your VPS container.</p>
    
    {message}

    <form method="POST" action="/submit_panel">
        
        <div class="card" style="border-left: 5px solid #dc3545;">
            <h3 style="margin-top:0; color: #dc3545;">1. Server Instance Configuration</h3>
            <p style="font-size: 0.9em; color: #666;">These parameters will save to <code>{config_file}</code> upon server shutdown.</p>
            {config_fields_html}
        </div>

        <div class="card">
            <h3 style="margin-top:0;">2. Single File Download</h3>
            <div class="form-group">
                <label for="url">File URL:</label>
                <input type="text" id="url" name="url" placeholder="https://example.com/file.zip">
            </div>
            <div class="form-group">
                <label for="name">Save Name (with extension):</label>
                <input type="text" id="name" name="name" placeholder="file.zip">
            </div>
            <div class="form-group">
                <label for="dir">Save Directory:</label>
                <input type="text" id="dir" name="dir" placeholder="./downloads">
            </div>
        </div>

        <div class="card">
            <h3 style="margin-top:0;">3. Batch JSON Download</h3>
            <div class="form-group">
                <label for="json_data">Paste JSON Array:</label>
                <textarea id="json_data" name="json_data" rows="6" placeholder='[\n  {{\n    "url": "https://example.com/file1.zip",\n    "name": "file1.zip",\n    "save_dir": "./downloads"\n  }}\n]'></textarea>
            </div>
        </div>

        <div class="card" style="background: #fdfdfd; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <button type="submit" name="action" value="download_single" class="btn btn-primary">Download Single</button>
                <button type="submit" name="action" value="download_batch" class="btn btn-success">Run Batch Job</button>
            </div>
            <button type="submit" name="action" value="shutdown" class="btn btn-danger" onclick="return confirm('Are you sure you want to save parameters and terminate the server session?');">Stop Server & Save Config</button>
        </div>
    </form>
</body>
</html>
"""

def download_file(url, name, save_dir):
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    full_path = os.path.join(save_dir, name)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response, open(full_path, 'wb') as out_file:
        out_file.write(response.read())
    return full_path

def load_saved_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def build_config_html(current_config):
    html = ""
    for key, value in current_config.items():
        label_text = key.replace("_", " ").title()
        html += f"""
        <div class="form-group">
            <label for="cfg_{key}">{label_text}:</label>
            <input type="text" id="cfg_{key}" name="cfg_{key}" value="{value}">
        </div>"""
    return html

class ControlPanelHandler(http.server.BaseHTTPRequestHandler):
    def render_panel(self, message_html=""):
        current_config = load_saved_config()
        fields_html = build_config_html(current_config)
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        response_content = HTML_FORM.format(
            message=message_html, 
            config_file=CONFIG_FILE, 
            config_fields_html=fields_html
        )
        self.wfile.write(response_content.encode('utf-8'))

    def do_GET(self):
        self.render_panel()

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        fields = urllib.parse.parse_qs(post_data)
        
        action = fields.get('action', [''])[0]
        message_html = ""
        
        updated_config = {}
        for key in DEFAULT_CONFIG.keys():
            updated_config[key] = fields.get(f'cfg_{key}', [''])[0].strip()

        if action == "shutdown":
            try:
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(updated_config, f, indent=4)
                print(f"[file_fetcher] Config saved to {CONFIG_FILE}")
            except Exception as e:
                print(f"[file_fetcher] Error writing config: {e}")
            
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body><h2>System Offline</h2><p>Saved parameters to <code>{CONFIG_FILE}</code>. Workflow run released.</p></body></html>".encode('utf-8'))
            threading.Thread(target=self.server.shutdown).start()
            return

        elif action == "download_single":
            url = fields.get('url', [''])[0].strip()
            name = fields.get('name', [''])[0].strip()
            save_dir = fields.get('dir', [''])[0].strip()
            if url and name and save_dir:
                try:
                    path = download_file(url, name, save_dir)
                    message_html = f'<div class="message success">Successfully downloaded:\n{path}</div>'
                except Exception as e:
                    message_html = f'<div class="message error">Single Download Error:\n{str(e)}</div>'
            else:
                message_html = '<div class="message error">Error: Single file fields are incomplete.</div>'

        elif action == "download_batch":
            json_str = fields.get('json_data', [''])[0].strip()
            if json_str:
                try:
                    tasks = json.loads(json_str)
                    results = []
                    success_count = 0
                    for i, item in enumerate(tasks):
                        if 'url' in item and 'name' in item and 'save_dir' in item:
                            try:
                                path = download_file(item['url'], item['name'], item['save_dir'])
                                results.append(f"[{i+1}/{len(tasks)}] SUCCESS: {item['name']}")
                                success_count += 1
                            except Exception as err:
                                results.append(f"[{i+1}/{len(tasks)}] FAILED: {item['name']} | {str(err)}")
                        else:
                            results.append(f"[{i+1}/{len(tasks)}] FAILED: Bad Item Formatting Keys")
                    status_cls = "success" if success_count == len(tasks) else "error"
                    message_html = f'<div class="message {status_cls}">Batch Job Result ({success_count}/{len(tasks)}):\n' + "\n".join(results) + '</div>'
                except Exception as e:
                    message_html = f'<div class="message error">JSON Processing Failure:\n{str(e)}</div>'
            else:
                message_html = '<div class="message error">Error: JSON text box is empty.</div>'

        # Persist config changes
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(updated_config, f, indent=4)
        except Exception:
            pass

        self.render_panel(message_html)

    def log_message(self, format, *args):
        return  # quiet

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), ControlPanelHandler) as httpd:
        print(f"[file_fetcher] Panel active on port {PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass