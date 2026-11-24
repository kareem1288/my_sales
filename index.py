"""Vercel entrypoint.

Vercel loads its entrypoint as a standalone file, not as part of a package, so
the app (which uses package-relative imports) is imported from here instead.

If the app cannot even be imported (missing dependency, bad configuration),
a minimal fallback app reports the error instead of letting the serverless
function crash with an opaque FUNCTION_INVOCATION_FAILED.
"""

import html
import os
import re
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
	sys.path.insert(0, ROOT)

try:
	from app.main import app  # noqa: E402
except Exception:  # pragma: no cover - only reached on a broken deployment
	_details = traceback.format_exc()
	print(_details, file=sys.stderr, flush=True)  # appears in the hosting provider's logs
	# Never echo credentials that may appear in connection strings.
	_details = re.sub(r"://[^/\s:@]+:[^@\s]+@", "://***:***@", _details)
	_page = (
		"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
		"<title>My Sales failed to start</title></head><body style='font-family:system-ui,sans-serif;padding:16px;max-width:60rem;margin:auto'>"
		"<h1>My Sales failed to start</h1><p>The application could not be loaded. Error details:</p>"
		f"<pre style='white-space:pre-wrap;background:#f4f4f4;padding:1rem;border-radius:8px'>{html.escape(_details)}</pre>"
		f"<p>Python {html.escape(sys.version)}</p></body></html>"
	).encode()

	async def app(scope, receive, send):
		if scope["type"] == "lifespan":
			while True:
				message = await receive()
				if message["type"] == "lifespan.startup":
					await send({"type": "lifespan.startup.complete"})
				elif message["type"] == "lifespan.shutdown":
					await send({"type": "lifespan.shutdown.complete"})
					return
		if scope["type"] != "http":
			return
		await send({"type": "http.response.start", "status": 500,
			"headers": [(b"content-type", b"text/html; charset=utf-8"), (b"cache-control", b"no-store")]})
		await send({"type": "http.response.body", "body": _page})

__all__ = ["app"]
