from __future__ import annotations
import json, os, queue, sys, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont, messagebox

TEXT = {
    'subtitle':'账号与 API 管理',
    'starting':'正在启动', 'running':'运行中',
    'stopped':'已停止', 'stopping':'正在停止',
    'failed':'启动失败', 'open':'打开控制台',
    'key':'复制管理密钥','copied':'已复制',
    'port':'本地端口','start':'启动服务',
    'stop':'停止服务','data':'数据目录',
    'port_locked':'停止后可修改',
    'port_invalid':'端口必须是 1024–65535 的整数',
    'quit':'退出网关',
    'quit_confirm':'退出后将停止 API 服务。是否继续？',
    'port_busy':'端口已被占用，请退出旧窗口或更换端口',
    'regions':'CN 国内   /   GL 国际',
    'save_failed':'无法保存端口设置',
}
COLORS={'bg':'#101318','surface':'#191e26','line':'#2a313d','text':'#edf1f7','muted':'#8c98aa','accent':'#3d79df','accent_hover':'#4a89f0','button':'#242c39','button_hover':'#303c4d','ok':'#66cda6','error':'#ed979c'}

def enable_dpi():
    if os.name=='nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError,AttributeError): pass

class Button(tk.Canvas):
    def __init__(self,parent,text,command,width=180,primary=False,font=None):
        self.caption=text; self.command=command; self.enabled=True; self.primary=primary; self.font=font; self.hover=False
        super().__init__(parent,width=width,height=38,bg=COLORS['bg'],highlightthickness=0,bd=0,takefocus=True,cursor='hand2')
        self.bind('<Configure>',lambda _:self.draw()); self.bind('<Enter>',lambda _:self.enter(True)); self.bind('<Leave>',lambda _:self.enter(False)); self.bind('<Button-1>',lambda _:self.invoke()); self.bind('<Return>',lambda _:self.invoke());self.bind('<space>',lambda _:self.invoke());self.bind('<FocusIn>',lambda _:self.draw());self.bind('<FocusOut>',lambda _:self.draw())
    def enter(self,value):self.hover=value;self.draw()
    def invoke(self):
        if self.enabled:self.command()
    def set(self,text=None,enabled=None):
        if text is not None:self.caption=text
        if enabled is not None:self.enabled=enabled;self.configure(cursor='hand2' if enabled else 'arrow')
        self.draw()
    def draw(self):
        self.delete('all');w=self.winfo_width();h=self.winfo_height();r=7
        fill=COLORS['accent_hover' if self.hover else 'accent'] if self.primary else COLORS['button_hover' if self.hover else 'button']
        if not self.enabled:fill=COLORS['surface']
        points=[r,1,w-r,1,w-1,1,w-1,r,w-1,h-r,w-1,h-1,w-r,h-1,r,h-1,1,h-1,1,h-r,1,r,1,1]
        self.create_polygon(points,smooth=True,splinesteps=20,fill=fill,outline=COLORS['muted'] if self.focus_get()==self else fill)
        self.create_text(w/2,h/2,text=self.caption,font=self.font,fill=COLORS['text'] if self.enabled else COLORS['muted'])

class Launcher:
    def __init__(self,args,service_factory,home,window=None,autostart=True):
        self.args=args;self.home=Path(home);self.window=window or tk.Tk();self.service=service_factory(args);self.phase='starting' if autostart else 'stopped';self.closing=False;self.error='';self.after_id=None
        w=self.window;w.title('WB Gateway');w.geometry('500x336');w.minsize(500,336);w.resizable(False,False);w.configure(bg=COLORS['bg'])
        families=set(tkfont.families(w));self.family=next((f for f in ('Microsoft YaHei UI','Microsoft YaHei','Noto Sans CJK SC','SimHei') if f in families),'TkDefaultFont');self.body_font=(self.family,10);self.small_font=(self.family,9)
        w.option_add('*Font',self.body_font);w.option_add('*selectBackground',COLORS['accent'])
        self.frame=tk.Frame(w,bg=COLORS['bg'],padx=24,pady=20);self.frame.pack(fill='both',expand=True)
        top=tk.Frame(self.frame,bg=COLORS['bg']);top.pack(fill='x')
        logo=tk.Label(top,text='W',font=('Segoe UI',13,'bold'),fg=COLORS['text'],bg=COLORS['button'],width=3,pady=5);logo.pack(side='left',padx=(0,12))
        names=tk.Frame(top,bg=COLORS['bg']);names.pack(side='left')
        self.label(names,'WB Gateway',font=('Segoe UI',15,'bold')).pack(anchor='w')
        self.label(names,TEXT['regions'],muted=True,font=self.small_font).pack(anchor='w',pady=(2,0))
        self.status=tk.Label(top,text=TEXT[self.phase],font=self.small_font,fg=COLORS['muted'],bg=COLORS['bg']);self.status.pack(side='right',anchor='n',pady=5)
        tk.Frame(self.frame,bg=COLORS['line'],height=1).pack(fill='x',pady=(18,14))
        card=tk.Frame(self.frame,bg=COLORS['surface'],padx=14,pady=12,highlightthickness=1,highlightbackground=COLORS['line']);card.pack(fill='x')
        self.label(card,'API Base URL',muted=True,font=self.small_font,bg=COLORS['surface']).pack(anchor='w')
        self.endpoint=tk.Label(card,text=self.api_url,font=('Consolas',11),fg=COLORS['text'],bg=COLORS['surface'],anchor='w');self.endpoint.pack(fill='x',pady=(5,0))
        actions=tk.Frame(self.frame,bg=COLORS['bg']);actions.pack(fill='x',pady=(14,12));actions.columnconfigure(0,weight=1);actions.columnconfigure(1,weight=1)
        self.open_button=Button(actions,TEXT['open'],self.open_ui,primary=True,font=self.body_font);self.open_button.grid(row=0,column=0,sticky='ew',padx=(0,5))
        self.key_button=Button(actions,TEXT['key'],self.copy_key,font=self.body_font);self.key_button.grid(row=0,column=1,sticky='ew',padx=(5,0))
        controls=tk.Frame(self.frame,bg=COLORS['bg']);controls.pack(fill='x');self.label(controls,TEXT['port'],muted=True,font=self.small_font).pack(side='left')
        self.port=tk.StringVar(value=str(args.port));self.port_box=tk.Entry(controls,textvariable=self.port,width=6,justify='center',font=('Consolas',10),bg=COLORS['surface'],fg=COLORS['text'],disabledbackground=COLORS['surface'],disabledforeground=COLORS['muted'],relief='flat',highlightthickness=1,highlightbackground=COLORS['line'],highlightcolor=COLORS['accent'],insertbackground=COLORS['text']);self.port_box.pack(side='left',padx=(9,10),ipady=5)
        self.toggle_button=Button(controls,TEXT['stop'],self.toggle,width=96,font=self.small_font);self.toggle_button.pack(side='right')
        self.data_button=tk.Label(controls,text=TEXT['data'],bg=COLORS['bg'],fg=COLORS['muted'],cursor='hand2',font=self.small_font,takefocus=True);self.data_button.pack(side='right',padx=(0,14));self.data_button.bind('<Button-1>',lambda _:self.open_data());self.data_button.bind('<Return>',lambda _:self.open_data())
        self.hint=tk.Label(self.frame,text='',bg=COLORS['bg'],fg=COLORS['muted'],font=self.small_font,anchor='w',wraplength=450);self.hint.pack(fill='x',pady=(8,0))
        w.protocol('WM_DELETE_WINDOW',self.close);self.render()
        if autostart:self.service.start()
        self.poll()
    @property
    def api_url(self): return f'http://127.0.0.1:{self.args.port}/v1'
    def label(self,parent,text,muted=False,font=None,bg=None):return tk.Label(parent,text=text,fg=COLORS['muted'] if muted else COLORS['text'],bg=bg or COLORS['bg'],font=font or self.body_font)
    def render(self):
        active=self.phase=='running';transition=self.phase in ('starting','stopping');self.status.configure(text=TEXT[self.phase],fg=COLORS['ok'] if active else COLORS['error'] if self.phase=='failed' else COLORS['muted']);self.endpoint.configure(text=self.api_url)
        self.port_box.configure(state='disabled' if active or transition else 'normal');self.toggle_button.set(text=TEXT['stop'] if active or self.phase=='starting' else TEXT['start'],enabled=not transition);self.open_button.set(enabled=active);self.key_button.set(enabled=active)
        self.hint.configure(text=self.error,fg=COLORS['error'])
        if self.error:
            self.hint.pack(fill='x',pady=(8,0));self.window.geometry('500x372')
        else:
            self.hint.pack_forget();self.window.geometry('500x336')
    def open_ui(self):
        if self.phase=='running':webbrowser.open(f'http://127.0.0.1:{self.args.port}/ui/')
    def copy_key(self):
        if self.phase=='running' and self.service.gateway:
            self.window.clipboard_clear();self.window.clipboard_append(self.service.gateway.admin_key);self.window.update();self.key_button.set(text=TEXT['copied']);self.window.after(1600,lambda:self.key_button.set(text=TEXT['key']))
    def open_data(self):
        path=Path(self.args.data_dir);path.mkdir(parents=True,exist_ok=True)
        if os.name=='nt':os.startfile(str(path.resolve()))
        else:webbrowser.open(path.resolve().as_uri())
    def toggle(self):
        if self.phase=='running':self.phase='stopping';self.service.stop();self.render();return
        if self.phase in ('starting','stopping'):return
        try:
            port=int(self.port.get());assert 1024<=port<=65535
        except (ValueError,AssertionError):self.error=TEXT['port_invalid'];self.render();return
        try:
            path=self.home/'launcher.json'
            try: saved=json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError,ValueError):saved={}
            path.write_text(json.dumps({**saved,'port':port},ensure_ascii=False),encoding='utf-8')
        except OSError:self.error=TEXT['save_failed'];self.render();return
        self.args.port=port;self.error='';self.phase='starting';self.service.start();self.render()
    def poll(self):
        try:
            while True:
                event,value=self.service.events.get_nowait()
                if event=='running':self.phase='running';self.error=''
                elif event=='error':self.phase='failed';self.error=TEXT['port_busy'] if any(v in value for v in ('10048','address already in use','Address already in use')) else value
                elif event=='stopped' and self.phase!='failed':self.phase='stopped'
                self.render()
        except queue.Empty:pass
        if self.closing and (not self.service.thread or not self.service.thread.is_alive()):self.window.destroy();return
        self.after_id=self.window.after(120,self.poll)
    def close(self):
        if self.phase in ('running','starting') and not messagebox.askokcancel(TEXT['quit'],TEXT['quit_confirm'],parent=self.window):return
        self.closing=True;self.service.stop()
    def snapshot(self):
        return {'title':self.window.title(),'phase':self.phase,'font':self.family,'port_label':TEXT['port'],'port':self.port.get(),'api_url':self.endpoint.cget('text'),'open_enabled':self.open_button.enabled,'key_enabled':self.key_button.enabled,'hint':self.hint.cget('text'),'geometry':self.window.geometry()}

def open_launcher(args,service_factory,home):
    enable_dpi();ui=Launcher(args,service_factory,home);ui.window.mainloop()
