"""Render real BOOP views against disposable local data; no Bluetooth or OS actions."""
import asyncio
import argparse
from pathlib import Path
import sys
import tempfile
import time
import math

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web
import boop
from analytics import AnalyticsService
from api import FeatureAPI
from boop import Manager, make_app
from companion import Companion
from features import FeatureStore
from storage import Store


class OfflineManager(Manager):
    def __init__(self,store):
        super().__init__(store)
        self.address='00:00:00:00:00:01'
        self.name='Isolated QA · no strap'

    async def scan(self):
        return []

    def connect(self, address):
        raise ValueError("This isolated QA preview has no Bluetooth connection")

    async def send(self, *args, **kwargs):
        raise ValueError("Hardware actions are disabled in the isolated QA preview")

    async def status(self):
        if getattr(self,'synthetic_demo',False):
            now=time.time()
            self.rr.clear()
            for i in range(250):
                stamp=now-(249-i)*.86
                self.rr.append((int(stamp*1000),860+48*math.sin(stamp*math.pi/2)))
        value=await super().status()
        if getattr(self,'synthetic_demo',False):
            value.update(hr=70,hr_age_s=0,battery=78,synthetic_demo=True,
                         phase='Synthetic demo samples · no Bluetooth connection')
        return value


async def main(demo=False,port=8766):
    with tempfile.TemporaryDirectory(prefix="boop-qa-") as directory:
        # Legacy SQLite export uses this module-level path. Keep it disposable
        # even though the offline manager is never started.
        boop.DATA=Path(directory)/'data'
        manager = OfflineManager(Store(Path(directory)/"data"/"whoop.sqlite"))
        features = FeatureStore(manager.store)
        if demo:
            from screenshot_demo import seed_demo
            seed_demo(manager.store,features,manager.address)
            manager.name='Synthetic demo · no strap'
            manager.phase='Synthetic demo · Bluetooth disabled'
            manager.synthetic_demo=True
        manager.features = features
        manager.companion = Companion(manager, features)
        stop = asyncio.Event()
        api = FeatureAPI(manager, features, AnalyticsService(manager.store), manager.companion)
        api.root = Path(directory)
        app = make_app(manager, stop, port, api)

        @web.middleware
        async def qa_label(request, handler):
            if request.path == "/":
                html = (ROOT/"web/index.html").read_text(encoding="utf-8")
                html = html.replace("BOOP · Your body, your data", "BOOP · Isolated QA preview")
                html = html.replace("Your body, your data.", "Isolated QA · disposable data.")
                if demo:
                    html=html.replace('Isolated QA preview','Synthetic demo data')
                    html=html.replace('Isolated QA · disposable data.','Synthetic demo data · no strap.')
                return web.Response(text=html, content_type="text/html")
            return await handler(request)

        app.middlewares.append(qa_label)

        @web.middleware
        async def responsive_preview(request, handler):
            response=await handler(request)
            if request.path=='/' and request.query.get('qa_frame')=='1':
                # Only disposable QA may embed itself for a fixed-width review.
                # The production app keeps frame-ancestors 'none'.
                policy=response.headers.get('Content-Security-Policy','')
                response.headers['Content-Security-Policy']=policy.replace("frame-ancestors 'none'","frame-ancestors 'self'")
            return response
        app.middlewares.insert(0,responsive_preview)

        async def mobile_preview(request):
            views={'today','sleep','activity','health','insights','tools','device','data','settings','coach'}
            view=request.query.get('view','today')
            if view not in views:raise web.HTTPBadRequest(text='Choose a BOOP page')
            return web.Response(text='<!doctype html><html lang="en"><head><meta charset="utf-8"><title>BOOP · Disposable responsive QA</title><link rel="stylesheet" href="/qa/responsive.css"></head><body><h1>BOOP · 390 px responsive review</h1><p>Disposable data · Bluetooth and OS actions disabled</p><iframe id="mobile-preview" title="BOOP phone-width preview" width="390" height="844" src="/?qa_frame=1#'+view+'"></iframe></body></html>',content_type='text/html')

        async def preview_style(request):
            return web.Response(text='body{margin:0;padding:26px;background:#dfe5dc;color:#26332d;text-align:center;font:14px Segoe UI,Arial,sans-serif}h1{font-size:18px;font-weight:500;margin:0 0 8px}p{margin:0 0 20px;color:#526456;font-size:12px}iframe{display:block;width:390px;height:844px;border:0;margin:auto;border-radius:14px;box-shadow:0 14px 50px #253b2825;background:#f2f1eb}',content_type='text/css')

        app.router.add_get('/qa/mobile',mobile_preview)
        app.router.add_get('/qa/responsive.css',preview_style)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        print(f"Isolated BOOP {'synthetic demo' if demo else 'QA preview'} http://127.0.0.1:{port}; Bluetooth and OS actions disabled", flush=True)
        try:
            await stop.wait()
        finally:
            await manager.companion.close()
            await runner.cleanup()


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo',action='store_true',help='Populate disposable synthetic screenshot examples')
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('Choose port 1024–65535')
    asyncio.run(main(args.demo,args.port))
