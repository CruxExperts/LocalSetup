"""One serialized pytest display owner; resource supervision stays independent."""
from functools import wraps
import os
import threading

from .branding.terminal import color_capable
from .test_telemetry import render, status


class TestOutput:
    def __init__(self, reporter, project, interval=5):
        self.reporter, self.project, self.interval = reporter, project, interval
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.originals = []
        self.output_stream = None

    def replace(self, owner, name, value):
        self.originals.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def start(self):
        reporter, writer = self.reporter, self.reporter._tw
        # Pytest redirects fd 1 during serial tests. Keep the display on the
        # original destination so timer output cannot enter a test's capture
        # buffer while still advancing the terminal writer's column counter.
        try:
            writer.flush()
            descriptor = os.dup(writer._file.fileno())
        except (AttributeError, OSError, ValueError):
            pass  # In-memory writers and sys capture already keep their sink.
        else:
            self.output_stream = os.fdopen(descriptor, "w", encoding=writer._file.encoding or "utf-8",
                                           errors="replace", buffering=1)
            self.replace(writer, "_file", self.output_stream)
        self.replace(writer, "fullwidth", 80)
        self.replace(reporter, "_screen_width", 80)
        original_write = writer.write
        @wraps(original_write)
        def write(*args, **kwargs):
            with self.lock:
                return original_write(*args, **kwargs)
        self.replace(writer, "write", write)

        def fill():
            with self.lock:
                message = reporter._get_progress_information_message()
                if writer.width_of_current_line + len(message) > 80:
                    writer.line()
                color, _ = reporter._get_main_color()
                reporter.write(message.rjust(80 - writer.width_of_current_line), flush=True, **{color: True})
        def edge():
            with self.lock:
                if writer.width_of_current_line + len(reporter._get_progress_information_message()) >= 80:
                    fill()
                    writer.line()
        self.replace(reporter, "_write_progress_information_filling_space", fill)
        self.replace(reporter, "_write_progress_information_if_past_edge", edge)
        self.thread = threading.Thread(target=self.loop, name="test-status-display", daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop.wait(self.interval):
            try:
                value = status(self.project)
                snapshot = value.get("snapshot") or {}
                if (value["status"] != "live" or snapshot.get("run_id") != os.environ.get("LOCALSETUP_TEST_RUN_ID")
                        or not (snapshot.get("progress") or {}).get("collection_known")):
                    continue
                # Theme resolution and file reads stay outside the output lock.
                text = render(snapshot, color=color_capable(self.reporter._tw._file))
                with self.lock:
                    if self.stop.is_set():
                        return
                    writer = self.reporter._tw
                    if writer.width_of_current_line:
                        if self.reporter._show_progress_info:
                            self.reporter._write_progress_information_filling_space()
                        writer.line()
                    writer.line(text)
                    self.reporter.flush()
            except (OSError, ValueError, RuntimeError):
                # Display failure cannot stop tests or their independent watchdog.
                continue

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        with self.lock:
            if self.reporter._tw.width_of_current_line:
                self.reporter._tw.line()
            for owner, name, value in reversed(self.originals):
                setattr(owner, name, value)
            if self.output_stream is not None:
                self.output_stream.close()
