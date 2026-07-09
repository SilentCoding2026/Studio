import json
import os
import urllib.request
import urllib.parse
import sys
import re

MODS_JSON = "mods.json"
MODS_DIR = "server/mods"

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def download_file(url, dest_path):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response, open(dest_path, 'wb') as out:
        out.write(response.read())
    print(f"  ✅ Downloaded: {os.path.basename(dest_path)}")

def resolve_modrinth_url(project_url, version_str):
    match = re.search(r'modrinth\.com/mod/([^/?]+)', project_url)
    if not match:
        return None
    slug = match.group(1)
    api_url = f"https://api.modrinth.com/v2/project/{slug}/version"
    try:
        with urllib.request.urlopen(api_url) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  ⚠️ API error: {e}")
        return None

    # Prefer exact match on version_number
    for v in data:
        if v.get('version_number') == version_str:
            for file in v.get('files', []):
                if file.get('primary'):
                    return file.get('url')
    # Fallback: check if version_str is contained (for versions like "1.2.3+build")
    for v in data:
        if version_str in v.get('version_number', ''):
            for file in v.get('files', []):
                if file.get('primary'):
                    return file.get('url')
    return None

def resolve_curseforge_url(project_url, version_str):
    # CurseForge requires an API key. We'll skip with a clear message.
    print(f"  ⚠️ CurseForge auto-resolve not supported (needs API key). Provide a direct .jar URL or download manually.")
    return None

def download_mods():
    if not os.path.exists(MODS_JSON):
        print("ℹ️ mods.json not found. Skipping mod download.")
        return

    with open(MODS_JSON, 'r') as f:
        mods = json.load(f)

    ensure_dir(MODS_DIR)

    for mod in mods:
        filename = mod.get('filename', '')
        if filename.endswith('.disabled'):
            print(f"⏭ Skipping disabled: {filename}")
            continue

        target_name = filename
        if target_name.endswith('.disabled'):
            target_name = target_name[:-9]

        url = mod.get('url', '').strip()
        version = mod.get('version', '')

        if not url:
            print(f"⚠️ No URL for {target_name}, skipping.")
            continue

        dest = os.path.join(MODS_DIR, target_name)
        if os.path.exists(dest):
            print(f"⏭ {target_name} already exists, skipping.")
            continue

        # Resolve if needed
        if 'modrinth.com' in url:
            resolved = resolve_modrinth_url(url, version)
            if resolved:
                url = resolved
            else:
                print(f"  ❌ Could not resolve Modrinth version for {target_name}. Skipping.")
                continue
        elif 'curseforge.com' in url:
            resolved = resolve_curseforge_url(url, version)
            if resolved:
                url = resolved
            else:
                # Skip CurseForge pages unless direct .jar link
                if not url.endswith('.jar'):
                    print(f"  ❌ Skipping CurseForge project page (no direct .jar link): {url}")
                    continue

        # Try to download
        try:
            print(f"⬇ Downloading {target_name} ...")
            download_file(url, dest)
        except Exception as e:
            print(f"  ❌ ERROR: {e}")

if __name__ == "__main__":
    download_mods()