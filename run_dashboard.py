"""Start the dashboard and open it in the browser. Used by START_WINDOWS.bat and start_mac.command."""
import os
import socket
import threading
import time
import webbrowser

PORT = int(os.environ.get("PORT", 8050))   # set PORT=8060 if 8050 is already in use
URL = f"http://127.0.0.1:{PORT}"


def open_when_ready():
    for _ in range(120):                      # wait up to 60 s for the server to come up
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
            webbrowser.open(URL)
            return
        except OSError:
            time.sleep(0.5)


if __name__ == "__main__":
    print(f"\n  Lead Router dashboard: {URL}\n  Close this window (or press Ctrl+C) to stop it.\n")
    threading.Thread(target=open_when_ready, daemon=True).start()
    from app.main import app
    app.run(debug=False, port=PORT)
