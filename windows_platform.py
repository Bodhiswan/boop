"""Windows counterparts for the local menu bar, startup and app notifications."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import time
import webbrowser

class WindowsPlatform:
    def __init__(self,manager,features,stop,port=8765):
        self.manager,self.features,self.stop,self.port=manager,features,stop,port
        self.root=Path(__file__).resolve().parent
        self.icon=self.loop=self.task=None
        self._last_notice={}
        self.awake_held=False

    def recording_power(self,enabled):
        """An app-scoped hold; display sleep and system power preferences stay intact."""
        if os.name!="nt" or self.awake_held==enabled:
            return
        import ctypes
        flags=0x80000000 | (0x00000001 if enabled else 0)
        if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
            self.manager.log("Windows could not hold the recording session awake")
            return
        self.awake_held=enabled

    def startup(self,enabled):
        if os.name!="nt":
            if enabled: raise ValueError("Windows startup is unavailable")
            return
        import winreg
        command=f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{self.root / "start.ps1"}" -NoBrowser'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if enabled: winreg.SetValueEx(key,"BOOP",0,winreg.REG_SZ,command)
            else:
                try: winreg.DeleteValue(key,"BOOP")
                except FileNotFoundError: pass
            # Remove the old application label when the startup preference is saved.
            try: winreg.DeleteValue(key,"BOOP Local WHOOP")
            except FileNotFoundError: pass

    async def start(self):
        self.loop=asyncio.get_running_loop()
        if os.name=="nt":
            import pystray
            from PIL import Image,ImageDraw
            image=Image.new("RGBA",(64,64),(35,43,39,255))
            draw=ImageDraw.Draw(image)
            draw.ellipse((12,12,52,52),fill=(149,185,155,255)); draw.ellipse((26,26,38,38),fill=(35,43,39,255))
            def open_app(icon,item): webbrowser.open(f"http://127.0.0.1:{self.port}")
            def sync(icon,item):
                future=asyncio.run_coroutine_threadsafe(self.manager.sync(),self.loop)
                future.add_done_callback(lambda f:self.manager.log(f"Tray sync: {f.exception()}" if f.exception() else "Tray requested history sync"))
            def quit_app(icon,item): self.loop.call_soon_threadsafe(self.stop.set)
            self.icon=pystray.Icon("boop",image,"BOOP",menu=pystray.Menu(
                pystray.MenuItem("Open BOOP",open_app,default=True),
                pystray.MenuItem("Sync strap history",sync,enabled=lambda item:self.manager.connected and not self.manager._sync_active),
                pystray.MenuItem("Quit BOOP",quit_app)))
            self.icon.run_detached()
        self.task=asyncio.create_task(self.watch())

    def notify(self,key,message):
        settings=self.features.settings()
        if not settings["notifications_enabled"] or not self.icon: return False
        from companion import zone
        from datetime import datetime
        current=datetime.now(zone(settings["timezone"])).strftime("%H:%M")
        start,end=settings["quiet_hours_start"],settings["quiet_hours_end"]
        quiet=(start<=current<end) if start<end else (current>=start or current<end) if start!=end else False
        if quiet or time.monotonic()-self._last_notice.get(key,-100000)<3600: return False
        self._last_notice[key]=time.monotonic(); self.icon.notify(message,"BOOP")
        return True

    async def watch(self):
        while True:
            settings=await asyncio.to_thread(self.features.settings)
            self.recording_power(self.manager.connected and settings["keep_laptop_awake"])
            if self.icon:
                hr=f"{self.manager.hr} bpm" if self.manager.connected and self.manager.hr else self.manager.phase
                battery=f" · {self.manager.battery:.0f}%" if self.manager.battery is not None else ""
                self.icon.title="BOOP · "+hr+battery
            if self.manager.battery is not None and self.manager.battery<=settings["battery_threshold"]:
                await asyncio.to_thread(self.notify,"battery","Your strap battery is low. Charge it to keep your record growing.")
            for record in await asyncio.to_thread(self.features.list_records,"reminder"):
                if record.get("enabled",True) in (False,0,"false","False"):
                    continue
                due=record.get("timestamp_ms") or record.get("due_ms")
                if isinstance(due,(int,float)) and 0<=time.time()*1000-due<60000:
                    await asyncio.to_thread(self.notify,"reminder:"+record["id"],str(record.get("title") or record.get("name") or "Your BOOP reminder")[:150])
            await asyncio.sleep(30)

    async def close(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
        self.recording_power(False)
        if self.icon: await asyncio.to_thread(self.icon.stop)
