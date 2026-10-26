"""Ask the local YT-API for meta of YouTube URLs and print formats/sizes.

Run from the project root: .venv/bin/python scripts/meta_probe.py <url> [<url> ...]
"""
import asyncio, sys, time
sys.path.insert(0, ".")
from app.services.nats_meta import create_nats_client, request_social_meta

URLS = sys.argv[1:]

async def main():
    nc = await create_nats_client()
    for url in URLS:
        t = time.monotonic()
        try:
            r = await request_social_meta(url, nc, user_id=1, message_id=1, user_language="uk")
        except Exception as exc:
            print(f"{url}: EXC {type(exc).__name__} {exc} after {time.monotonic()-t:.1f}s"); continue
        dt = time.monotonic() - t
        print(f"{url}: result={r.get('result')} err={r.get('error_code')} {str(r.get('error'))[:80]} in {dt:.1f}s | title={r.get('title','')[:40]!r} dur={r.get('duration')}")
        for v in r.get("download_urls", []):
            size = v.get("file_size") or v.get("file_size_approx")
            print(f"   {v['format_id']:>7} {v['label']:>6} {v['height']:>5} size={size/1e6 if size else 0:8.1f}MB exact={bool(v.get('file_size'))}")
    await nc.close()

asyncio.run(main())
