"""Embedded operation-bound remote sudo runtime for checksum-staged wrappers."""

REMOTE_SUDO_SUPPORT = r'''
class SudoProxy:
    def __init__(self, spec, argv, cwd, deadline, *, stdin=subprocess.DEVNULL, capture_tail=False):
        import io,threading,tempfile,zipfile
        self.stdin=stdin
        self.capture_tail=capture_tail
        self.returncode=None; self.pid=None; self.sudo_state=None; self.result=None
        self.captured_stdout=b""; self.captured_stderr=b""; self.stdout_truncated=False; self.stderr_truncated=False
        self.done=threading.Event()
        self.stdout_read,stdout_write=os.pipe()
        self.stderr_read,stderr_write=os.pipe()
        os.close(stdout_write); os.close(stderr_write)
        self.stdout=os.fdopen(self.stdout_read,"rb",buffering=0)
        self.stderr=os.fdopen(self.stderr_read,"rb",buffering=0)
        required={"nonce","runtime_b64","runtime_sha256","target","binding_key","single_password_qualified"}
        if not isinstance(spec,dict) or set(spec)!=required: raise ValueError("invalid sudo operation binding")
        nonce=spec["nonce"]
        if not isinstance(nonce,str) or len(nonce)!=32 or any(c not in REQUEST for c in nonce): raise ValueError("invalid sudo nonce")
        if type(spec["single_password_qualified"]) is not bool: raise ValueError("invalid sudo authentication qualification")
        target=text(spec["target"],"sudo target",128)
        binding_key=text(spec["binding_key"],"managed binding key",64)
        if len(binding_key)!=64 or set(binding_key)-SHA: raise ValueError("invalid managed binding key")
        runtime_digest=text(spec["runtime_sha256"],"sudo runtime digest",64)
        if len(runtime_digest)!=64 or set(runtime_digest)-SHA: raise ValueError("invalid sudo runtime digest")
        runtime=base64.b64decode(spec["runtime_b64"].encode("ascii"),validate=True)
        if len(runtime)>256*1024 or hashlib.sha256(runtime).hexdigest()!=runtime_digest: raise ValueError("sudo runtime digest mismatch")
        self.runtime=tempfile.TemporaryDirectory(prefix="fleetops-sudo-runtime-",dir="/tmp")
        os.chmod(self.runtime.name,0o700)
        allowed={"ls/__init__.py","ls/core/__init__.py","ls/core/terminal_ops/__init__.py",
                 "ls/core/terminal_ops/sudo.py","ls/core/terminal_ops/credentials.py",
                 "ls/core/terminal_ops/sudo_channel.py","ls/core/terminal_ops/dialog_channel.py",
                 "ls/core/terminal_ops/command_state.py","ls/core/terminal_ops/dialog_model.py",
                 "ls/core/terminal_ops/shell.py","ls/core/terminal_ops/models.py",
                 "ls/core/terminal_ops/askpass.py","ls/tools/sudo_askpass"}
        with zipfile.ZipFile(io.BytesIO(runtime)) as archive:
            if set(archive.namelist())!=allowed: raise ValueError("sudo runtime members mismatch")
            for member in archive.infolist():
                if member.file_size>256*1024 or member.is_dir(): raise ValueError("sudo runtime member invalid")
                path=os.path.join(self.runtime.name,member.filename)
                os.makedirs(os.path.dirname(path),mode=0o700,exist_ok=True)
                with open(path,"xb") as stream: stream.write(archive.read(member))
                os.chmod(path,0o500 if member.filename=="ls/tools/sudo_askpass" else 0o400)
        sys.path.insert(0,self.runtime.name)
        os.environ["PYTHONPATH"]=self.runtime.name+os.pathsep+os.environ.get("PYTHONPATH","")
        from ls.core.terminal_ops.sudo import OperationBinding,payload_digest,run_sudo
        self.OperationBinding=OperationBinding; self.payload_digest=payload_digest; self.run_sudo=run_sudo
        self.argv=argv; self.cwd=cwd; self.deadline=deadline; self.spec=spec; self.nonce=nonce; self.target=target; self.binding_key=binding_key
        self.directory="/tmp/fleetops-sudo-auth-"+nonce
        os.mkdir(self.directory,0o700)
        os.chmod(self.directory,0o700)
        self.fifo=self.directory+"/candidate"
        os.mkfifo(self.fifo,0o600)
        os.chmod(self.fifo,0o600)
        self.parent_pid=os.getppid()
        try:
            with open("/proc/"+str(self.parent_pid)+"/stat","rb") as stream: self.parent_start=stream.read(4096).split()[21]
        except (OSError,IndexError): raise ValueError("sudo parent generation unavailable")
        print("FLEETOPS_SUDO_BRIDGE_READY "+request_id+" "+nonce,flush=True)
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()
    def _current(self):
        try:
            with open("/proc/"+str(self.parent_pid)+"/stat","rb") as stream:
                same=stream.read(4096).split()[21]==self.parent_start
            return same and maybe_read(rootfd,claim_name,MAX_RECEIPT)==canonical(claim)
        except (OSError,IndexError,ValueError): return False
    def _candidate(self,name):
        print("FLEETOPS_SUDO_CANDIDATE "+request_id+" "+self.nonce+" "+name,flush=True)
        fd=os.open(self.fifo,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
        data=bytearray(); limit=time.monotonic()+20.0
        try:
            while time.monotonic()<limit and len(data)<=4098 and self._current():
                try: chunk=os.read(fd,4099-len(data))
                except BlockingIOError: chunk=b""
                if chunk: data.extend(chunk)
                if b"\n" in data: break
                time.sleep(0.01)
            if (len(data)<2 or len(data)>4098 or not data.endswith(b"\n")
                or data.count(b"\n")!=1 or b"\0" in data or b"\r" in data): return None
            if data[:1]==b"S" and len(data)==2: return None
            if data[:1]!=b"O": return None
            value=data[1:-1].decode("utf-8","strict")
            return value if value else None
        finally:
            for index in range(len(data)): data[index]=0
            os.close(fd)
    def _resolver(self,target,environment):
        from ls.core.terminal_ops.credentials import candidate_names
        for name in candidate_names(target):
            value=self._candidate(name)
            if value is not None: yield name,value
    def _callback(self,state):
        if state.get("event")=="sudo" and state.get("status")=="started":
            self.pid=state.get("sudo_pid")
        if not self._current(): raise RuntimeError("sudo owner changed")
    def _record_sudo(self):
        directory=os.path.join(SUDO_RECEIPT_ROOT,self.binding_key)
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            import stat
            info=os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
                raise PermissionError("managed sudo receipt directory is not private")
            receipt=dict(self.sudo_state); receipt["operation_id"]=request_id
            descriptor=os.open(request_id+".sudo.json",os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
                json.dump(receipt,stream,sort_keys=True,separators=(",",":"))
                stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        finally: os.close(fd)
    def _run(self):
        import threading
        # Drain both pipes throughout the command. Bound retained output, not
        # the sudo child's file size: its workload may legitimately write a
        # large backup or download to an unrelated file.
        captures=[]
        for name in ("stdout","stderr"):
            read_fd,write_fd=os.pipe()
            capture={"name":name,"read":read_fd,"write":write_fd,"data":bytearray(),"truncated":False}
            def drain(item):
                try:
                    while True:
                        chunk=os.read(item["read"],65536)
                        if not chunk: break
                        if self.capture_tail:
                            item["data"].extend(chunk)
                            if len(item["data"])>MAX_OUTPUT:
                                del item["data"][:-MAX_OUTPUT]; item["truncated"]=True
                        else:
                            room=MAX_OUTPUT-len(item["data"])
                            if room>0: item["data"].extend(chunk[:room])
                            if len(chunk)>room: item["truncated"]=True
                except OSError:
                    item["truncated"]=True
                finally: os.close(item["read"])
            # A live unknown workload must retain a reader even after the
            # controller deadline. Non-daemon drainers keep this visible
            # wrapper alive until every inherited output descriptor closes.
            thread=threading.Thread(target=drain,args=(capture,),daemon=False)
            capture["thread"]=thread; thread.start(); captures.append(capture)
        try:
            binding=self.OperationBinding(
                operation_id=request_id,owner_id=remote["session"],target=self.target,
                endpoint_id=remote["endpoint"],live_owner_id=remote["process_generation"],
                payload_digest=self.payload_digest(self.argv),validate_current=self._current,
                single_password_qualified=self.spec["single_password_qualified"])
            result=self.run_sudo(binding,self.argv,environment=os.environ,resolver=self._resolver,
                                 stdin=self.stdin,stdout=captures[0]["write"],
                                 stderr=captures[1]["write"],
                                 cwd=self.cwd,timeout=max(1.0,self.deadline-5.0),state_callback=self._callback)
            self.result=result; self.sudo_state=result.safe_state()
            self.returncode=result.returncode if type(result.returncode) is int else 75
            self._record_sudo()
        except BaseException:
            self.returncode=75
            self.sudo_state={"status":"unknown","detail":"remote sudo runtime could not be reconciled"}
        finally:
            for capture in captures:
                os.close(capture["write"])
                capture["thread"].join(timeout=1.0)
                setattr(self,"captured_"+capture["name"],bytes(capture["data"]))
                setattr(self,capture["name"]+"_truncated",capture["truncated"])
            def cleanup():
                for capture in captures: capture["thread"].join()
                try: os.unlink(self.fifo)
                except OSError: pass
                try: os.rmdir(self.directory)
                except OSError: pass
                self.runtime.cleanup()
            if any(capture["thread"].is_alive() for capture in captures):
                threading.Thread(target=cleanup,daemon=False).start()
            else: cleanup()
            self.done.set()
    def poll(self): return self.returncode if self.done.is_set() else None
    def wait(self,timeout=None):
        if not self.done.wait(timeout): raise subprocess.TimeoutExpired("sudo",timeout)
        return self.returncode
    def kill(self):
        # Unknown sudo is never interrupted by a replay-oriented wrapper.
        raise PermissionError("sudo outcome requires read-only reconciliation")
'''
