"""Explicit tool-free heartbeat review through the registered completion owner."""
import json
from pathlib import Path

from heartbeat_lscli import _resolve
from heartbeat_protocol import _object, _constant

PATHS = ('executable', 'profiles', 'runtime_root', 'grant')


def plan(profile, agent, name, target_root):
    required = set(PATHS) | {'client', 'launcher', 'profile', 'prompt', 'context',
                             'timeout_seconds', 'token_limit', 'output_limit_bytes'}
    if (set(profile) != required or profile['client'] != 'lscli'
            or profile['launcher'] != 'lscli-review'
            or set(agent) - {'enabled', 'profile', 'timeout_seconds'}
            or agent.get('enabled') is not True):
        raise ValueError('Review requires exactly its documented fields')
    paths = {}
    for key in PATHS:
        value = profile[key]
        if (not isinstance(value, str) or not value or '\0' in value
                or not Path(value).is_absolute() or '..' in Path(value).parts
                or str(Path(value)) != value):
            raise ValueError('Review paths must be absolute canonical strings')
        paths[key] = Path(value)
    if not isinstance(target_root, Path) or not target_root.is_absolute():
        raise ValueError('Review requires an explicit target')
    timeout = agent.get('timeout_seconds', profile['timeout_seconds'])
    for value, low, high in ((timeout, 1, 3600), (profile['token_limit'], 1, 1000000),
                             (profile['output_limit_bytes'], 1024, 4194304)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Review limits must be bounded integers')
    selected, prompt = profile['profile'], profile['prompt']
    if not isinstance(selected, str) or not 1 <= len(selected) <= 256 or '\0' in selected:
        raise ValueError('Review requires a named provider profile')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > 65536:
        raise ValueError('Review requires a bounded prompt')
    # The same qualified framework owner used by coding binds installed dispatch.
    command = _resolve(paths['executable'], paths['runtime_root'])
    from ls.core.agent.completion_context import validate_paths
    from ls.core.agent.profiles import load
    from ls.core.agent.review_contract import SCHEMA
    validate_paths(profile['context'])
    provider = load(paths['profiles'], selected)
    # validate_only does not send a native response-format parameter. Give the
    # model the same contract that the local receipt will require.
    response_format = ('Return only a JSON object, without Markdown fences. Response schema: '
                       + json.dumps(SCHEMA, sort_keys=True, separators=(',', ':')))
    request = {'interface_version': 1, 'model': provider.model, 'max_attempts': 1,
               'deadline_seconds': timeout, 'max_output_tokens': profile['token_limit'],
               'input': prompt + '\n\n' + response_format,
               'output_schema': SCHEMA, 'schema_mode': 'validate_only'}
    payload = json.dumps(request, ensure_ascii=False)
    if len(payload.encode('utf-8')) > 131072:
        raise ValueError('Review request exceeds heartbeat input limit')
    command += ['complete', '--profile=' + selected, '--request', '-',
                '--profiles', str(paths['profiles']), '--runtime-root', str(paths['runtime_root']),
                '--grant', str(paths['grant']), '--workspace', str(target_root), '--timeout', str(timeout)]
    for path in profile['context']:
        command += ['--context=' + path]
    return {'id': name + '-agent', 'command': command, 'policy_command': command,
            'allow_direct': False, 'stdin_text': payload, 'timeout_seconds': timeout + 20,
            'protocol_options': {'mode': 'completion', 'model': provider.model,
                                 'output_limit': profile['output_limit_bytes']},
            'launcher_info': {'client': 'lscli', 'launcher_mode': 'lscli-review',
                              'profile': name, 'prompt_transport': 'stdin',
                              'resolved_executable': command[0]}}


class Receipt:
    """Validate one completion envelope; retain only bounded advisory output."""
    def __init__(self, model):
        self.model, self.buffer, self.result = model, bytearray(), None

    def feed(self, raw):
        if self.result is not None:
            raise ValueError('Bytes after completion result')
        self.buffer.extend(raw)
        if len(self.buffer) > 1048576 + 4096:
            raise ValueError('Completion envelope exceeds limit')
        if not self.buffer.endswith(b'\n'):
            return False
        try:
            value = json.loads(self.buffer.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)
            from ls.core.agent.completion_run import accept
            from ls.core.agent.review_contract import validate
            # Bootstrap failure may not yet know the model. It cannot succeed.
            accept(value, self.model if value.get('model') is not None else None)
            if value['status'] == 'succeeded':
                if value['model'] != self.model:
                    raise ValueError('Completion model differs')
                validate(value['data'])
            self.result = value
        except (ValueError, TypeError, AttributeError, RecursionError) as exc:
            raise ValueError('Invalid review completion') from exc
        self.buffer.clear()
        return False

    def finish(self, returncode):
        if self.buffer or self.result is None:
            raise ValueError('Missing completion result')
        result = {'completed': returncode == 0 and self.result['status'] == 'succeeded',
                  'status': self.result['status'], 'mode': 'read-only-review'}
        if result['completed']:
            result['review'] = self.result['data']
        return result
