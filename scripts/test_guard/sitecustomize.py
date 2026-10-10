"""Disposable-test access guard, inherited by Python child processes."""
import os,sys
from pathlib import Path
if os.environ.get('PE_RELEASE_GUARD')=='1':
    repo=Path(os.environ['PE_RELEASE_REPO']).resolve()
    temp=Path(os.environ['PE_RELEASE_TEMP']).resolve()
    python_roots=tuple(Path(p).resolve() for p in (sys.prefix,sys.base_prefix))
    allowed=tuple(repo/p for p in ('src','scripts'))
    def check(value):
        if isinstance(value,int) or value is None:return
        try:value=os.fsdecode(value)
        except TypeError:return
        if value in (':memory:','','/dev/null','/dev/urandom','/etc/localtime','/private/etc/localtime'):return
        if value.startswith('file:'):value=value[5:].split('?')[0]
        path=Path(os.path.normpath(str(Path(value).absolute())))
        if path.is_relative_to(temp):return
        if any(path.is_relative_to(p) for p in (*python_roots,*allowed)):return
        if path==repo/'pyproject.toml':return
        if str(path).startswith(('/System/Library/','/usr/lib/','/Library/Apple/')):return
        raise PermissionError('RELEASE_EXTERNAL_ACCESS_DENIED')
    def audit(event,args):
        if event in ('open','sqlite3.connect','os.listdir','os.scandir') and args:check(args[0])
        if event in ('socket.connect','socket.bind'):raise PermissionError('RELEASE_NETWORK_DENIED')
    sys.addaudithook(audit)
