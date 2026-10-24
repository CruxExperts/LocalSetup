"""Owner delivery controls independent of worker cancellation and disk writes."""
import threading


class Delivery:
    def __init__(self):
        self.paused = threading.Event()
        self.buffer = None

    def bind(self, buffer):
        if self.buffer is not None:
            raise ValueError('Event delivery is already bound')
        self.buffer = buffer

    def request(self, value):
        method = value['method']
        common = {'schema_version', 'id', 'method'}
        if method == 'events.read':
            if set(value) != common | {'after', 'limit', 'max_bytes'}:
                raise ValueError('Invalid event read fields')
            if type(value['max_bytes']) is not int or not 1024 <= value['max_bytes'] <= 32768:
                raise ValueError('Control event page exceeds reply limit')
        elif method not in ('events.status', 'events.pause', 'events.resume') or set(value) != common:
            raise ValueError('Invalid event control')
        if method == 'events.pause':
            self.paused.set()
        elif method == 'events.resume':
            self.paused.clear()
        buffer = self.buffer
        if buffer is None:
            return dict(state='not_ready', paused=self.paused.is_set())
        if method == 'events.read':
            return buffer.read(**{key: value[key] for key in ('after', 'limit', 'max_bytes')})
        return dict(state='ready', paused=self.paused.is_set(), task=buffer.task,
                    session=buffer.session, run=buffer.run, publication_pending=buffer.failed)
