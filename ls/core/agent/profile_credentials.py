"""Explicit credential source declarations; resolution belongs to the bootstrap."""
from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class CredentialSource:
    provider: str
    name: str
    envman_binary: str | None = None
    protected_file_root: str | None = None

    def __post_init__(self):
        if (not isinstance(self.provider, str) or self.provider not in {'env', 'envman', 'protected-file'}
                or not isinstance(self.name, str)
                or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', self.name)):
            raise ValueError('Invalid explicit credential source')
        for path in (self.envman_binary, self.protected_file_root):
            if path is not None and (not isinstance(path, str) or not path or len(path) > 4096
                    or '\0' in path or not Path(path).is_absolute() or '..' in Path(path).parts
                    or str(Path(path)) != path):
                raise ValueError('Credential source paths must be bounded absolute canonical strings')
        if self.envman_binary is not None and self.provider != 'envman':
            raise ValueError('Only Envman sources accept an executable')
        if (self.protected_file_root is not None) != (self.provider == 'protected-file'):
            raise ValueError('Protected-file credentials require their explicit private root')


def parse(value) -> CredentialSource:
    if (not isinstance(value, dict) or not {'provider', 'name'} <= value.keys()
            or value.keys() - {'provider', 'name', 'envman_binary', 'protected_file_root'}
            or any(value[key] is None for key in value)):
        raise ValueError('Invalid credential source fields')
    return CredentialSource(**value)


def wire(source: CredentialSource) -> dict:
    result = {'provider': source.provider, 'name': source.name}
    if source.envman_binary is not None:
        result['envman_binary'] = source.envman_binary
    if source.protected_file_root is not None:
        result['protected_file_root'] = source.protected_file_root
    return result


def resolve(profile) -> str:
    """Return one credential privately; never search, fall back, cache or persist."""
    source = profile.credential_source
    if source is None:
        return profile.credential({profile.credential_env: os.environ.get(profile.credential_env, '')})
    from ..openpgp.secrets import SecretReference, SecretResolver
    resolver = SecretResolver(envman_binary=source.envman_binary or 'envman',
                              protected_file_root=source.protected_file_root)
    value = resolver.resolve(SecretReference(source.provider, source.name))
    return profile.credential({profile.credential_env: value})
