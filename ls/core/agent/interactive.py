"""Plain terminal owner interface over existing steering and approval authority."""
import json
import os
from queue import Empty, Full, Queue
import select
import threading
import time

from .run_io import Streams


class Terminal(Streams):
    def __init__(self, expires, cancelled, **kwargs):
        super().__init__(expires,cancelled,**kwargs)
        if not os.isatty(self.input_fd) or not os.isatty(self.output_fd):
            raise ValueError('Interactive input and output must be terminals')
        self.stop = threading.Event()
        self.thread = None
        self.notice_thread = None
        self.notices = Queue(maxsize=32)
        self.output_lock = threading.RLock()
        self.pending_lock = threading.Lock()
        self.pending = None
        self.steering = self.approvals = None
        self.buffer = b''
        self.input_bytes = 0

    def check(self):
        if self.stop.is_set():
            raise InterruptedError('Terminal input stopped')
        super().check()

    def write(self, text):
        with self.output_lock:
            super().write(text)

    def _notice(self, text):
        # Owner command handling must never wait for terminal output or its lock.
        # These are fixed acknowledgements, not model text or approval previews.
        self.check()
        if type(text) is not str or len(text.encode()) > 4096:
            raise ValueError('Terminal acknowledgement exceeds limit')
        try:
            self.notices.put_nowait(text)
        except Full:
            self.cancelled.set()
            raise ValueError('Terminal acknowledgement queue is full') from None

    def _write_notices(self):
        try:
            while not self.stop.is_set() and not self.cancelled.is_set():
                try:
                    text = self.notices.get(timeout=0.05)
                except Empty:
                    continue
                self.write(text)
        except (OSError, ValueError, TypeError, RuntimeError):
            if not self.stop.is_set() and time.monotonic() < self.expires:
                self.cancelled.set()

    def line(self):
        while b'\n' not in self.buffer:
            self.check()
            if self.stop.is_set():
                raise InterruptedError('Terminal input stopped')
            if not select.select([self.input_fd],[],[],0.02)[0]:
                continue
            chunk = os.read(self.input_fd,4096)
            if not chunk:
                raise InterruptedError('Terminal owner disconnected')
            self.input_bytes += len(chunk)
            self.buffer += chunk
            if self.input_bytes > 256*1024 or len(self.buffer)>16384:
                raise ValueError('Terminal input budget exceeded')
        raw,self.buffer = self.buffer.split(b'\n',1)
        return raw.decode().removesuffix('\r')

    def prompt(self):
        self.write('Enter a multiline task. Type /send on its own line to submit; /cancel cancels.\n')
        lines=[];size=0
        while True:
            line=self.line()
            if line=='/cancel':
                raise InterruptedError('Task cancelled')
            if line=='/send':
                break
            size+=len(line.encode())+1
            if size>128*1024:
                raise ValueError('Prompt exceeds 128 KiB')
            lines.append(line)
        prompt='\n'.join(lines)
        if not prompt.strip():
            raise ValueError('Task must not be empty')
        self.notice_thread=threading.Thread(target=self._write_notices,name='agent-terminal-output',daemon=True)
        self.thread=threading.Thread(target=self._commands,name='agent-terminal',daemon=True)
        self.notice_thread.start()
        self.thread.start()
        self._notice('Running. /steer TEXT sends additional text to this provider; /cancel stops work.\n')
        return prompt

    def approval(self, packet):
        with self.pending_lock:
            self.pending=packet
        self.write('Tool approval request (JSON data):\n'+json.dumps(packet,ensure_ascii=True,sort_keys=True)+'\n'
                   +'/approve '+packet['challenge']+' or /deny '+packet['challenge']+'\n')

    def _commands(self):
        try:
            while not self.stop.is_set():
                line=self.line()
                if line=='/cancel':
                    self.cancelled.set();return
                if line.startswith('/steer '):
                    task,session,profile=self.steering.identity
                    self.steering.accept(dict(schema_version=1,id=1,method='steer',task=task,session=session,
                                              profile=profile,text=line[7:],disclose=True))
                    self._notice('Steering queued; delivery is not yet confirmed.\n')
                elif line.startswith(('/approve ','/deny ')):
                    command,challenge=line.split(' ',1)
                    with self.pending_lock:
                        packet=self.pending
                        if packet is None or packet['challenge']!=challenge:
                            self._notice('No matching pending approval.\n');continue
                        decision={key:packet[key] for key in ('task','session','profile','challenge','sha256')}
                        self.approvals.decide(dict(decision,schema_version=1,id=1,method='approve',allow=command=='/approve'))
                        self.pending=None
                    self._notice('Decision recorded; execution remains subject to current grants.\n')
                else:
                    self._notice('Use /steer TEXT, /approve CHALLENGE, /deny CHALLENGE or /cancel.\n')
        except (OSError,ValueError,TypeError,RuntimeError):
            if not self.stop.is_set() and time.monotonic()<self.expires:
                self.cancelled.set()

    def close(self):
        self.stop.set()
        deadline=time.monotonic()+1
        for thread in (self.thread, self.notice_thread):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=max(0,deadline-time.monotonic()))
        if any(thread is not None and thread.is_alive() for thread in (self.thread,self.notice_thread)):
            raise RuntimeError('Terminal input or output did not stop')
