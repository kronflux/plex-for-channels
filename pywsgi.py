"""
pywsgi.py - Main server application for Plex channel proxy and EPG generation.

This module provides a Flask web server and scheduling system for:
1. Serving Plex live TV channels via m3u playlists
2. Generating and serving EPG (Electronic Program Guide) data
3. Managing streaming URL proxying
4. HLS segment proxying
"""

from gevent import monkey
monkey.patch_all()

from gevent.pywsgi import WSGIServer
from flask import Flask, redirect, request, Response, send_file, abort
import os
import importlib
import schedule
import time
import urllib.parse
import requests
import io
from threading import Thread, Event, Lock
from urllib.parse import urljoin

# Application version information
VERSION = "5.0.0"
UPDATED_DATE = "April 17 2025"

# Configure port from environment or use default
try:
    PORT = int(os.environ.get("PORT", 7777))
except (ValueError, TypeError):
    PORT = 7777

# Initialize Flask application
app = Flask(__name__)

# === In-memory state for proxy functionality ===
stream_map = {}
logo_cache = {}
logo_dir = "./logos"
segment_base_map = {}  # Stores base URLs for HLS streams
map_lock = Lock()

# Configure supported providers
PROVIDER_LIST = ['plex']
providers = {}

# Load all provider modules
for provider in PROVIDER_LIST:
    providers[provider] = importlib.import_module(provider).Client()

# Store trigger events for EPG generation
trigger_events = {}

# Base HTML template for the index page
url_main = f'''<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Plex for Channels</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bulma@1.0.3/css/bulma.min.css">
  </head>
  <body>
  <section class="section py-2">
      <h1 class="title is-2">
        Plex for Channels
        <span class="tag">v{VERSION}</span>
        <span class="tag">Last Updated: {UPDATED_DATE}</span>
      </h1>'''

# === Utility Functions ===

def get_proxy_base_url():
    """
    Returns the base URL for the currently active Flask request context.
    This ensures dynamically correct host:port usage.
    """
    return request.host_url.rstrip('/')

def rewrite_hls_playlist(playlist_content, slug, base_url):
    """
    Rewrite HLS playlist to point segments to our proxy
    """
    lines = playlist_content.split('\n')
    rewritten = []
    
    for line in lines:
        # Skip comments and empty lines
        if line.startswith('#') or not line.strip():
            rewritten.append(line)
            continue
            
        # Rewrite .ts segments
        if line.endswith('.ts'):
            # Extract just the segment number (e.g. "123.ts" -> "123")
            segment_num = line.split('/')[-1].split('.')[0]
            rewritten.append(f"/segment/{slug}/{segment_num}.ts")
        # Rewrite variant playlists
        elif line.endswith('.m3u8'):
            # Extract the slug from the original URL
            orig_slug = line.split('/')[-1].split('.')[0]
            rewritten.append(f"/stream/{orig_slug}")
        else:
            rewritten.append(line)
            
    return '\n'.join(rewritten)

# === EPG Generation Management ===

def trigger_epg_build(provider):
    """
    Manually trigger EPG generation for a specific provider.
    """
    if provider in trigger_events:
        trigger_events[provider].set()
    else:
        print(f"[ERROR - {provider}] No scheduler thread found for provider: {provider}")

def epg_scheduler(provider):
    """
    Execute EPG generation for a provider with error handling.
    """
    print(f"[INFO - {provider.upper()}] Running EPG Scheduler for {provider}")

    try:
        error = providers[provider].epg()
        if error:
            print(f"[ERROR - {provider.upper()}] EPG: {error}")
    except Exception as e:
        print(f"[ERROR - {provider.upper()}] Exception in EPG Scheduler: {e}")
    
    print(f"[INFO - {provider.upper()}] EPG Scheduler Complete")

def scheduler_thread(provider):
    """
    Run a continuous scheduler for EPG generation.
    """
    # Initialize provider trigger event if not already present
    if provider not in trigger_events:
        trigger_events[provider] = Event()

    event = trigger_events[provider]

    # Configure schedule based on provider type
    match provider.lower():
        case 'plex':
            schedule.every(10).minutes.do(lambda: epg_scheduler(provider))
        case _:
            schedule.every(1).hours.do(lambda: epg_scheduler(provider))

    # Initial EPG generation on startup
    while True:
        try:
            epg_scheduler(provider)
            break  # Continue to main loop after successful initial run
        except Exception as e:
            print(f"[ERROR - {provider.upper()}] Error in initial run, retrying: {e}")
            time.sleep(10)  # Brief delay before retry
            continue

    # Main scheduler loop
    while True:
        try:
            # Run scheduled tasks
            schedule.run_pending()

            # Check for manual trigger
            if event.is_set():
                print(f"[MANUAL TRIGGER - {provider.upper()}] Running epg_scheduler manually...")
                epg_scheduler(provider)
                event.clear()  # Reset event after execution

            time.sleep(1)
            
        except Exception as e:
            print(f"[ERROR - {provider.upper()}] Error in scheduler thread: {e}")
            break  # Exit this loop to restart EPG generation

def monitor_thread(provider):
    """
    Monitor and restart the scheduler thread if it fails.
    """
    def thread_wrapper(provider):
        print(f"[INFO - {provider.upper()}] Starting Scheduler thread for {provider}")
        scheduler_thread(provider)

    thread = Thread(target=thread_wrapper, args=(provider,), daemon=True)
    thread.start()

    # Continuously monitor thread health
    while True:
        if not thread.is_alive():
            print(f"[ERROR - {provider.upper()}] Scheduler thread stopped. Restarting...")
            thread = Thread(target=thread_wrapper, args=(provider,), daemon=True)
            thread.start()
        
        time.sleep(15 * 60)  # Check every 15 minutes
        print(f"[INFO - {provider.upper()}] Checking scheduler thread")

# === Proxy Routes ===

@app.route('/register', methods=['POST'])
def register():
    """
    Accepts POSTed JSON body with {slug: stream_url} entries and merges them into the stream_map.
    """
    new_map = request.get_json(force=True)
    with map_lock:
        stream_map.update(new_map)
    return "OK", 200

@app.route('/stream/<slug>')
def stream(slug):
    """
    Redirects to the stream URL associated with the given slug.
    Also stores HLS base URL if this is an m3u8 stream.
    """
    with map_lock:
        url = stream_map.get(slug)
    if not url:
        return abort(404)
    
    # If this is an HLS stream, store the base URL for segment proxying
    if url.endswith('.m3u8'):
        base_url = url.rsplit('/', 1)[0] + '/'
        with map_lock:
            segment_base_map[slug] = base_url
    
    return redirect(url, code=302)

@app.route('/segment/<slug>/<index>.ts')
def segment_proxy(slug, index):
    """
    Proxy individual .ts segments for HLS streams
    """
    with map_lock:
        base_url = segment_base_map.get(slug)
        if not base_url:
            return abort(404)
    
    # Reconstruct the real segment URL
    segment_url = urljoin(base_url, f"{index}.ts")
    
    try:
        headers = {}
        if request.headers.get('Range'):
            headers['Range'] = request.headers['Range']

        # Stream the segment directly to the client
        req = requests.get(segment_url, headers=headers, stream=True)
        return Response(
            req.iter_content(chunk_size=1024),
            content_type=req.headers['content-type'],
            status=req.status_code,
            headers={
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'public, max-age=86400'  # Cache segments for 24 hours
            }
        )
    except Exception as e:
        print(f"[SEGMENT ERROR] {e}")
        return abort(502)

@app.route('/hls/<slug>.m3u8')
def hls_playlist(slug):
    """
    Proxy and rewrite HLS playlists
    """
    with map_lock:
        url = stream_map.get(slug)
        if not url:
            return abort(404)
    
    try:
        # Fetch the original playlist
        response = requests.get(url)
        response.raise_for_status()
        
        # Rewrite the playlist contents
        base_url = url.rsplit('/', 1)[0] + '/'
        rewritten = rewrite_hls_playlist(response.text, slug, base_url)
        
        return Response(
            rewritten,
            content_type='application/vnd.apple.mpegurl',
            headers={
                'Cache-Control': 'public, max-age=300'  # Cache for 5 minutes
            }
        )
    except Exception as e:
        print(f"[HLS PLAYLIST ERROR] {e}")
        return abort(502)

@app.route('/logo/<channel_id>.png')
def logo(channel_id):
    """
    Serves channel logos, downloading and caching locally if needed.
    Falls back to a placeholder if fetching fails.
    """
    filepath = os.path.join(logo_dir, f"{channel_id}.png")
    client = providers.get('plex')

    # Serve cached logo if available
    if os.path.exists(filepath):
        return send_file(filepath, mimetype="image/png")

    os.makedirs(logo_dir, exist_ok=True)

    # Get a valid token from any region
    token = next(
        (data.get("access_token") for data in client.token_keychain.values() if "access_token" in data),
        None
    )
    if not token:
        print(f"[WARNING] No token available for logo fetch for {channel_id}")
        return create_or_serve_placeholder(channel_id)

    # Prepare headers from provider config
    headers = client.headers.copy()
    headers.update({
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Origin': 'https://app.plex.tv',
        'Referer': 'https://app.plex.tv/',
        'Sec-Fetch-Dest': 'image',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
        'sec-ch-ua': '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"macOS"',
        'x-plex-token': token or '',
        'x-plex-client-identifier': client.device_id
    })

    # Upstream logo URL patterns
    url_patterns = [
        f"https://provider-static.plex.tv/epg/channels/logos/gracenote/{channel_id}.png",
        f"https://provider-static.plex.tv/2/epg/channels/logos/gracenote/{channel_id}.png",
        f"https://provider-static.plex.tv/proxy?url=https://gracenote-h.akamaihd.net/i/tvlogos/tms/{channel_id}_h3_aa.png"
    ]

    for url in url_patterns:
        try:
            print(f"[DEBUG] Trying to fetch logo from {url}")
            response = requests.get(url, timeout=5, headers=headers, allow_redirects=False)

            # Follow manual redirect if present
            if response.status_code == 302 and "Location" in response.headers:
                redirected_url = response.headers["Location"]
                print(f"[DEBUG] Redirected to: {redirected_url}")
                try:
                    response = requests.get(redirected_url, timeout=5, headers=headers)
                except Exception as e:
                    print(f"[DEBUG] Error fetching redirected logo: {e}")
                    continue

            if response.status_code == 200 and response.content:
                # Log if fallback CDN logo was used
                if "epg/cms/production" in url or "epg/cms/production" in response.url:
                    print(f"[INFO] Fallback CDN logo used for {channel_id}: {response.url}")

                with open(filepath, "wb") as f:
                    f.write(response.content)
                print(f"[INFO] Successfully cached logo for {channel_id}")
                return send_file(filepath, mimetype="image/png")
            else:
                print(f"[DEBUG] Failed to fetch logo from {url}: Status {response.status_code}")

        except Exception as e:
            print(f"[DEBUG] Error fetching logo from {url}: {e}")

    # All attempts failed
    print(f"[WARNING] All logo fetch attempts failed for {channel_id}. Using placeholder.")
    return create_or_serve_placeholder(channel_id)

def create_or_serve_placeholder(channel_id):
    """Create or serve a placeholder logo image."""
    placeholder_path = os.path.join(logo_dir, f"placeholder_{channel_id[0:8]}.png")
    default_placeholder = os.path.join(logo_dir, "placeholder.png")
    
    # If this specific placeholder already exists, serve it
    if os.path.exists(placeholder_path):
        return send_file(placeholder_path, mimetype="image/png")
    
    # If the default placeholder exists, serve it
    if os.path.exists(default_placeholder):
        return send_file(default_placeholder, mimetype="image/png")
    
    # Create a simple placeholder
    try:
        from PIL import Image, ImageDraw, ImageFont
        
        width, height = 400, 225
        img = Image.new('RGB', (width, height), color=(73, 109, 137))
        draw = ImageDraw.Draw(img)

        # Add channel ID text
        short_id = channel_id.split('-')[0][:8] if '-' in channel_id else channel_id[:8]

        try:
            font = ImageFont.truetype("arial.ttf", 40)
        except:
            font = ImageFont.load_default()

        # Calculate centered position
        text_width, text_height = draw.textsize(short_id, font=font)
        position = ((width - text_width) // 2, (height - text_height) // 2)

        draw.text(position, short_id, fill=(255, 255, 255), font=font)

        # Save as both specific and default placeholders
        img.save(placeholder_path)
        img.save(default_placeholder)
        return send_file(placeholder_path, mimetype="image/png")
        
    except ImportError:
        print(f"[WARNING] PIL not available. Creating empty placeholder for {channel_id}")
        with open(placeholder_path, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')
        return send_file(placeholder_path, mimetype="image/png")
    except Exception as e:
        print(f"[ERROR] Failed to create placeholder: {e}")
        return abort(404)

@app.route('/proxy')
def proxy_index():
    """
    Displays a simple HTML interface listing registered stream routes and fallback endpoints.
    """
    rows = [
        f"<tr><td>{slug}</td><td><a href='/stream/{slug}'>/stream/{slug}</a></td></tr>"
        for slug in stream_map.keys()
    ]
    table = "<table border='1'><tr><th>Slug</th><th>Stream Path</th></tr>" + ''.join(rows) + "</table>"

    html = f"""
    <html><head><title>Plex-for-Channels Proxy</title></head><body>
    <h1>Proxy Index</h1>
    <ul>
        <li><a href='/playlist.m3u'>/playlist.m3u</a></li>
        <li><a href='/epg.xml'>/epg.xml</a></li>
    </ul>
    <h2>Registered Streams</h2>
    {table}
    </body></html>
    """
    return html

# === Fallback routes for playlist and EPG (for Jellyfin compatibility) ===
@app.route('/playlist.m3u')
def default_playlist():
    """
    Redirects to the default region (local) playlist.
    """
    return redirect(f"{get_proxy_base_url()}/plex/playlist.m3u?regions=local", code=302)

@app.route('/playlist-<region>.m3u')
def region_playlist(region):
    """
    Redirects to a region-specific playlist.
    """
    return redirect(f"{get_proxy_base_url()}/plex/playlist.m3u?regions={region}", code=302)

@app.route('/epg.xml')
def default_epg():
    """
    Serves the generated EPG XML file directly with correct content type.
    """
    file_path = 'data/plex/epg.xml'
    try:
        return send_file(
            file_path, 
            mimetype='application/xml; charset=utf-8',
            headers={'Cache-Control': 'public, max-age=3600'}  # Cache for 1 hour
        )
    except FileNotFoundError:
        return "EPG is still being generated. Please try again shortly.", 503

@app.route('/epg-<region>.xml')
def region_epg(region):
    """
    Serves region-specific EPG files directly with correct headers.
    """
    file_path = f"data/plex/epg-{region}.xml"
    try:
        return send_file(file_path, mimetype='application/xml; charset=utf-8')
    except FileNotFoundError:
        return "EPG is still being generated. Please try again shortly.", 503

# === Main App Routes ===

@app.post("/register")
def register_proxy_map():
    """Register stream URLs with the proxy service."""
    try:
        data = request.get_json()
        with map_lock:
            stream_map.update(data)
        return "Proxy map updated", 200
    except Exception as e:
        return f"Failed to register proxy map: {e}", 500

@app.route("/")
def index():
    """Render the main index page with provider options."""
    host = request.host
    body = ''
    
    for provider in providers:
        geo_code_name = f"{provider.upper()}_CODE"
        geo_code_list = os.environ.get(geo_code_name)

        body += '<div>'
        body_text = providers[provider].body_text(provider, host, geo_code_list)
        body += body_text
        body += "</div>"
        
    return f"{url_main}{body}</section></body></html>"

@app.route("/<provider>/token")
def token(provider):
    """Get auth tokens for a provider."""
    args = request.args
    token_keychain, error = providers[provider].token(args)
    
    if error:
        return error
    else:
        return token_keychain

@app.get("/<provider>/playlist.m3u")
def playlist(provider):
    """Generate and serve M3U playlist for a provider."""
    args = request.args
    host = request.host

    m3u, error = providers[provider].generate_playlist(provider, args, host)
    
    if error: 
        return error, 500
        
    response = Response(m3u, content_type='audio/x-mpegurl')
    return response

@app.get("/<provider>/channels.json")
def channels_json(provider):
    """Return channel information as JSON."""
    args = request.args
    stations, err = providers[provider].channels(args)
    
    if err: 
        return err
        
    return stations

@app.get("/<provider>/rebuild_epg")
def rebuild_epg(provider):
    """Trigger a complete rebuild of the EPG data."""
    providers[provider].rebuild_epg()
    trigger_epg_build(provider)
    return "Rebuilding EPG"

@app.get("/<provider>/build_epg")
def build_epg(provider):
    """Trigger EPG generation without rebuilding."""
    trigger_epg_build(provider)
    return "Manually Triggering EPG"

@app.route("/<provider>/watch/<id>")
def watch(provider, id):
    """Redirect to the video stream for a specific channel."""
    video_url, err = providers[provider].generate_video_url(id)
    
    if err: 
        return "Error", 500, {'X-Tuner-Error': err}
        
    if not video_url:
        return "Error", 500, {'X-Tuner-Error': 'No Video Stream Detected'}
        
    return redirect(video_url)

@app.get("/<provider>/<filename>")
def epg_xml(provider, filename):
    """Serve EPG XML or compressed files."""
    file_path = f'data/{provider}/{filename}'

    try:
        suffix = filename.split('.')[-1] if '.' in filename else ''
        
        # Determine appropriate response based on file type
        if suffix.lower() == 'xml':
            return send_file(file_path, as_attachment=False, 
                            download_name=filename, mimetype='text/plain')
        elif suffix.lower() == 'gz':
            return send_file(file_path, as_attachment=True, 
                            download_name=filename)
        else:
            return f"{file_path} file not found", 404
            
    except FileNotFoundError:
        return "XML Being Generated Please Standby", 404


# === Server Startup ===

if __name__ == '__main__':
    # Initialize main trigger event
    trigger_event = Event()

    # Create logos directory if it doesn't exist
    os.makedirs(logo_dir, exist_ok=True)

    # Start monitoring threads for all providers
    for provider in PROVIDER_LIST:
        Thread(target=monitor_thread, args=(provider,), daemon=True).start()

    # Start the WSGI server
    try:
        print(f"[INFO - MAIN] ⇨ http server started on [::]:{PORT}")
        WSGIServer(('', PORT), app, log=None).serve_forever()
    except OSError as e:
        print(f"[ERROR - MAIN] Server failed to start: {e}")
