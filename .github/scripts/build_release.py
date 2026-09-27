from pathlib import Path
import base64,hashlib,json,os,platform,re,socket,struct,subprocess,sys,tempfile,time,urllib.request
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from wb_gateway import __version__
from wb_gateway.crypto import AESGCM
from PyInstaller.archive.readers import CArchiveReader
version=os.environ['RELEASE_VERSION'];arch=os.environ['RELEASE_ARCH'];actual=platform.machine().lower();expected={'x64':0x8664,'arm64':0xAA64}[arch]
assert version==__version__ and all(x.isdigit() for x in version.split('.')) and len(version.split('.'))==3
assert actual in (('arm64','aarch64') if arch=='arm64' else ('amd64','x86_64'))
import tkinter
w=tkinter.Tk();w.withdraw();w.update();w.destroy()
known=bytes.fromhex('cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919')
assert AESGCM(bytes(32)).encrypt(bytes(12),bytes(16),b'')==known
assert AESGCM(bytes(32)).decrypt(bytes(12),known,b'')==bytes(16)
name=f'WB-Gateway-v{version}-windows-{arch}';out=ROOT/'release';out.mkdir(exist_ok=True)
excluded=['cryptography','cffi','_cffi_backend','pytest','unittest','doctest','pydoc','pip','setuptools','distutils','test','idlelib','Crypto.SelfTest','Crypto.PublicKey','Crypto.Protocol','Crypto.IO','Crypto.Signature']
excluded += ['Crypto.Cipher._mode_'+m for m in ['cbc','cfb','ofb','openpgp','ccm','eax','siv','ocb']]
cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile',('--console' if os.environ.get('WB_RELEASE_DEBUG') else '--windowed'),'--noupx','--optimize','2','--name',name,'--distpath',str(out),'--workpath',str(ROOT/'build'/arch),'--specpath',str(ROOT/'build'),'--additional-hooks-dir',str(ROOT/'.github/pyinstaller-hooks'),'--add-data',str(ROOT/'web')+';web','--add-data',str(ROOT/'config')+';config','--collect-data','tzdata']
for m in excluded:cmd.extend(['--exclude-module',m])
cmd.append(str(ROOT/'app.py'))
if os.environ.get('WB_RELEASE_SKIP_BUILD')!='1':subprocess.run(cmd,cwd=ROOT,check=True)
exe=out/(name+'.exe')
def machine(raw):
 p=struct.unpack_from('<I',raw,0x3c)[0];assert raw[p:p+4]==bytes([80,69,0,0]);return struct.unpack_from('<H',raw,p+4)[0]
assert machine(exe.read_bytes())==expected
archive=CArchiveReader(str(exe));native={}
for filename in archive.toc:
 low=filename.lower();assert not any(x in low for x in ['master.key','accounts.enc.json','api-keys.json','call-logs','_rust','cryptography'])
 if low.endswith(('.dll','.pyd')):
  raw=archive.extract(filename)
  if raw[:2]==b'MZ':
   value=machine(raw);native[filename]=hex(value);assert value in ({expected,0xA641,0xA64E} if arch=='arm64' else {expected}),filename
with tempfile.TemporaryDirectory(prefix='wb-release-smoke-') as td:
 data=Path(td);master=os.urandom(32);iv=os.urandom(12);sealed=AESGCM(master).encrypt(iv,b'[]',b'workbuddy-proxy-v1');enc=lambda v:base64.b64encode(v).decode()
 (data/'master.key').write_text(enc(master),encoding='ascii');(data/'accounts.enc.json').write_text(json.dumps(dict(version=1,iv=enc(iv),data=enc(sealed[:-16]),tag=enc(sealed[-16:]))),encoding='utf-8')
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();p=subprocess.Popen([str(exe),'--headless','--host','127.0.0.1','--port',str(port),'--data-dir',td],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));health=None
 try:
  for _ in range(120):
   assert p.poll() is None,'Frozen executable exited early'
   try:health=json.load(opener.open(f'http://127.0.0.1:{port}/health',timeout=1));break
   except Exception:time.sleep(.5)
  assert health and health['version']==version,health
  assert opener.open(f'http://127.0.0.1:{port}/ui/',timeout=5).status==200
 finally:
  if p.poll() is None:subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],capture_output=True,check=True)
  stdout,stderr=p.communicate(timeout=30)
  (out/'smoke-process.json').write_text(json.dumps(dict(exit_status=p.returncode,stdout=stdout.decode(errors='replace'),stderr=stderr.decode(errors='replace')),indent=2),encoding='utf-8')
size=exe.stat().st_size;report=dict(version=version,architecture=arch,runner_machine=actual,python=sys.version,bytes=size,sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),native_modules=native,health=health,aes_gcm_known_vector=True,encrypted_store_startup=True,ui_http_status=200)
(out/(name+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(dict(file=exe.name,mib=round(size/1024**2,2),sha256=report['sha256']),indent=2))
