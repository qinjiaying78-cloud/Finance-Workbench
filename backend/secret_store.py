"""Write-only credential configuration; encrypted at rest, never returned to a browser."""
import json,os,re,threading
from pathlib import Path
from cryptography.fernet import Fernet
_lock=threading.Lock()
def folder(): return Path(os.getenv('FINANCE_SECRET_DIR',str(Path(__file__).parent/'private')))
def validate(ref):
    if not isinstance(ref,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{2,70}',ref): raise ValueError('密钥引用只允许大写字母、数字和下划线')
def cipher(create=False):
    root=folder();key=os.getenv('FW_MASTER_KEY')
    if not key:
        path=root/'master.key'
        if not path.exists():
            if not create: raise ValueError('尚未配置服务端凭据')
            root.mkdir(parents=True,exist_ok=True)
            try:
                with path.open('xb') as f: f.write(Fernet.generate_key())
            except FileExistsError: pass
            path.chmod(0o600)
        key=path.read_text().strip()
    return Fernet(key.encode())
def get_secret(book,ref):
    validate(ref)
    configured=os.getenv('FW_SECRET_'+ref)
    if configured: return json.loads(configured)
    root=folder();path=root/'credentials.enc'
    if not path.exists(): raise ValueError('尚未配置服务端凭据：'+ref)
    data=json.loads(cipher().decrypt(path.read_bytes()))
    value=data.get(book+':'+ref)
    if not value: raise ValueError('当前账套尚未配置凭据：'+ref)
    return value
def put_secret(book,ref,value):
    validate(ref)
    if not isinstance(value,dict) or len(json.dumps(value))>60000: raise ValueError('凭据须为 JSON 对象且小于 60 KB')
    with _lock:
        f=cipher(True);root=folder();root.mkdir(parents=True,exist_ok=True);path=root/'credentials.enc'
        data=json.loads(f.decrypt(path.read_bytes())) if path.exists() else {}
        data[book+':'+ref]=value
        tmp=root/'credentials.next';tmp.write_bytes(f.encrypt(json.dumps(data).encode()));tmp.chmod(0o600);tmp.replace(path)

