from __future__ import annotations
import argparse, asyncio, json, os, sys, threading, queue, webbrowser
from pathlib import Path
from aiohttp import web
from wb_gateway.server import Gateway, create_app

ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
HOME=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
class DataLock:
    def __init__(self,path):
        path.mkdir(parents=True,exist_ok=True); self.file=(path/'.python-gateway.lock').open('a+b'); self.file.seek(0); self.file.write(b'0'); self.file.flush(); self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt; msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl; fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError: self.file.close(); raise RuntimeError('数据目录已被另一个 Python 网关使用。')
    def close(self): self.file.close()
class Service:
    def __init__(self,args): self.args=args; self.events=queue.Queue(); self.loop=None; self.stop_event=None; self.thread=None; self.gateway=None
    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread=threading.Thread(target=self.worker,name='python-gateway',daemon=True); self.thread.start()
    def worker(self):
        try: asyncio.run(self.serve())
        except Exception as e: self.events.put(('error',str(e)))
    async def serve(self):
        self.loop=asyncio.get_running_loop(); self.stop_event=asyncio.Event(); lock=DataLock(Path(self.args.data_dir)); runner=None
        try:
            self.gateway=Gateway(ROOT,self.args.data_dir); runner=web.AppRunner(create_app(self.gateway),access_log=None,shutdown_timeout=10); await runner.setup(); await web.TCPSite(runner,self.args.host,self.args.port).start()
            self.events.put(('running',f'http://127.0.0.1:{self.args.port}/ui/')); await self.stop_event.wait()
        finally:
            if runner: await runner.cleanup()
            lock.close(); self.events.put(('stopped',''))
    def stop(self):
        if self.loop and self.loop.is_running() and self.stop_event: self.loop.call_soon_threadsafe(self.stop_event.set)

def gui(args):
    from wb_gateway.launcher_ui import open_launcher
    open_launcher(args,Service,HOME)

def main():
    try: saved=json.loads((HOME/'launcher.json').read_text(encoding='utf-8-sig'))
    except (OSError,ValueError): saved={}
    parser=argparse.ArgumentParser(description='Native Python WorkBuddy gateway'); parser.add_argument('--headless',action='store_true'); parser.add_argument('--host',default=os.environ.get('WB_HOST','127.0.0.1')); parser.add_argument('--port',type=int,default=int(os.environ.get('WB_PORT',str(saved.get('port',8787))))); parser.add_argument('--data-dir',default=os.environ.get('WB_DATA_DIR',str(HOME/'data'))); args=parser.parse_args()
    if args.headless:
        lock=DataLock(Path(args.data_dir))
        try: web.run_app(create_app(Gateway(ROOT,args.data_dir)),host=args.host,port=args.port,access_log=None,print=None)
        finally: lock.close()
    else: gui(args)
if __name__=='__main__': main()
