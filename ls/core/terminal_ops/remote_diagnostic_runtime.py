"""Nonsecret receiver for the shared one-off remote sudo operation."""

from .remote_sudo_runtime import REMOTE_SUDO_SUPPORT

# This wrapper supplies the same narrow owner/claim contract as the FleetOps
# adapter. Authentication remains in the shared sudo runtime and channel.
REMOTE_DIAGNOSTIC = r'''
import base64,builtins,hashlib,json,os,stat,subprocess,sys,time
from pathlib import Path
REQUEST=set("0123456789abcdef"); SHA=REQUEST; MAX_OUTPUT=16384; MAX_RECEIPT=1048576

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()

def text(value,label,limit):
    if not isinstance(value,str) or not value or len(value)>limit or any(ord(c)<32 for c in value):
        raise ValueError("invalid "+label)
    return value

def maybe_read(fd,name,limit):
    try:
        descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
        try:
            info=os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>limit:
                raise ValueError("unsafe operation file")
            return os.read(descriptor,limit+1)
        finally: os.close(descriptor)
    except FileNotFoundError: return None

def save(name,value):
    temporary=name+".tmp."+str(os.getpid())
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=rootfd)
    try:
        data=canonical(value)
        with os.fdopen(descriptor,"wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.rename(temporary,name,src_dir_fd=rootfd,dst_dir_fd=rootfd)
    finally:
        try: os.unlink(temporary,dir_fd=rootfd)
        except FileNotFoundError: pass

def generation(pid):
    raw=Path("/proc/"+str(pid)+"/stat").read_text()
    parts=raw[raw.rfind(")")+2:].split()
    if parts[0] in ("Z","X","x"): raise ValueError("owner exited")
    return parts[19]

root=Path(sys.argv[1]); action=sys.argv[2]; expected=sys.argv[3]
rootfd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
info=os.fstat(rootfd)
if info.st_uid!=os.getuid() or info.st_mode&0o077: raise ValueError("unsafe operation root")
claim_name="claim.json"; raw=maybe_read(rootfd,claim_name,MAX_RECEIPT)
if raw is None or hashlib.sha256(raw).hexdigest()!=expected: raise ValueError("claim mismatch")
claim=json.loads(raw)
request_id=claim["operation_id"]; nonce=claim["sudo"]["nonce"]
if len(request_id)!=32 or set(request_id)-REQUEST: raise ValueError("operation identity invalid")
remote=claim["remote"]
boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
if hashlib.sha256((boot+":"+str(os.getuid())).encode()).hexdigest()!=remote["process_generation"]:
    raise ValueError("remote machine generation changed")
SUDO_RECEIPT_ROOT=str(root/"sudo")
if action=="observe":
    result_raw=maybe_read(rootfd,"result.json",MAX_RECEIPT)
    if result_raw is not None:
        result=json.loads(result_raw)
        if result.get("claim_sha256")!=expected: raise ValueError("receipt mismatch")
        builtins.print(json.dumps({"receipt":result})); raise SystemExit(0)
    observed_raw=maybe_read(rootfd,"observation.json",MAX_RECEIPT)
    if observed_raw is None:
        builtins.print(json.dumps({"state":"staged","wrapper_current":False})); raise SystemExit(0)
    observed=json.loads(observed_raw)
    try:
        pid=observed["pid"]
        actual=Path("/proc/"+str(pid)+"/cmdline").read_bytes().split(b"\0")[:-1]
        wanted=[str(root),"run",expected]
        current=(generation(pid)==observed["start"] and len(actual)==6
                 and [os.fsdecode(item) for item in actual[3:]]==wanted
                 and actual[1]==b"-c"
                 and hashlib.sha256(actual[2]).hexdigest()==claim["runner_sha256"])
    except (OSError,ValueError,KeyError,IndexError): current=False
    builtins.print(json.dumps({"state":"running","wrapper_current":current,
                              "challenge":observed.get("challenge"),"claim_sha256":expected}))
    raise SystemExit(0)
if action!="run": raise ValueError("unsupported operation")
# Exclusive claim is the dispatch fence. A second run never executes sudo.
fd=os.open("started",os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=rootfd); os.close(fd)
observation={"pid":os.getpid(),"start":generation(os.getpid()),"challenge":None}
save("observation.json",observation)

def print(*items,**options):
    message=" ".join(str(item) for item in items)
    prefix="FLEETOPS_SUDO_CANDIDATE "+request_id+" "+nonce+" "
    if message.startswith(prefix):
        name=message[len(prefix):]
        if name!="GENERIC_SUDO_KEY": raise ValueError("unsupported credential candidate")
        observation["challenge"]=name; save("observation.json",observation)
    # Only fixed protocol events reach the pane, never workload bytes.
    builtins.print(message,flush=True)

# SHARED_SUDO_SUPPORT

os.mkdir(root/"sudo",0o700)
os.mkdir(root/"sudo"/claim["sudo"]["binding_key"],0o700)
proxy=SudoProxy(claim["sudo"],claim["argv"],claim["cwd"],claim["timeout"],stdin=sys.stdin.buffer,capture_tail=True)
while proxy.poll() is None: time.sleep(0.05)
state=proxy.sudo_state or {"status":"unknown"}
# An authentication or observation deadline is not command completion.
terminal=(state.get("status") in ("completed","failed","action_required","blocked","unsupported_authentication","unavailable"))
result={"operation_id":request_id,"claim_sha256":expected,"sudo":state,
        "exit_code":proxy.returncode,"state":"completed" if terminal else "unknown",
        "stdout_b64":base64.b64encode(proxy.captured_stdout).decode(),
        "stderr_b64":base64.b64encode(proxy.captured_stderr).decode(),
        "stdout_truncated":proxy.stdout_truncated,"stderr_truncated":proxy.stderr_truncated}
save("result.json",result)
raise SystemExit(proxy.returncode)
'''.replace("# SHARED_SUDO_SUPPORT", REMOTE_SUDO_SUPPORT)
