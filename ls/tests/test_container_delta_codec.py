"""Real bounded patches preserve exact text, binary and archive bytes."""
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import shutil
import zipfile

import pytest

from ls.core.openpgp.container_delta_codec import DeltaCodec, DeltaCodecError, create_patch, apply_patch
from ls.core.openpgp.container_delta_records import FileEvidence
from ls.core.openpgp.container_preparation import _Budget, ContainerPreparationLimits


def evidence(data):
    return FileEvidence(len(data), hashlib.sha256(data).hexdigest())


def private(path, data):
    path.write_bytes(data)
    path.chmod(0o600)
    return path


@pytest.fixture
def codec():
    executable = shutil.which('zstd')
    if not executable:
        pytest.skip('Operator-selected zstd is unavailable')
    path = Path(executable).resolve()
    return DeltaCodec(path, hashlib.sha256(path.read_bytes()).hexdigest(),
                      max_input_bytes=2**20, max_output_bytes=2**20)


def budget():
    return _Budget(ContainerPreparationLimits(timeout_seconds=10))


@pytest.mark.parametrize('kind', ['text', 'binary', 'zip'])
def test_real_patch_roundtrip_and_wrong_inputs_do_not_publish(tmp_path, codec, kind):
    before = bytes(range(256))*1024 if kind != 'text' else b'line repeated\n'*16000
    after = before[:8192]+b'changed bytes'+before[8210:]+b'tail'
    if kind == 'zip':
        def archive(data):
            value = io.BytesIO()
            with zipfile.ZipFile(value, 'w', compression=zipfile.ZIP_STORED) as out:
                out.writestr('file', data)
            return value.getvalue()
        before, after = archive(before), archive(after)
    base = private(tmp_path/'base', before)
    result = private(tmp_path/'result', after)
    patch = tmp_path/'patch'
    patch_claim = create_patch(base, result, patch, base=evidence(before), result=evidence(after),
                               policy=codec, budget=budget())
    assert patch_claim and patch_claim.length < len(after)
    assert patch.stat().st_mode & 0o777 == 0o600
    output = tmp_path/'output'
    assert apply_patch(base, patch, output, base=evidence(before), patch=patch_claim,
                       result=evidence(after), policy=codec, budget=budget()) == evidence(after)
    assert output.read_bytes() == after
    assert not list(tmp_path.glob('.ls-openpgp-*'))
    with pytest.raises(DeltaCodecError, match='result_mismatch'):
        apply_patch(base, patch, tmp_path/'wrong-result', base=evidence(before), patch=patch_claim,
                    result=FileEvidence(len(after), '0'*64), policy=codec, budget=budget())
    assert not (tmp_path/'wrong-result').exists()
    with pytest.raises(DeltaCodecError, match='input_changed'):
        apply_patch(base, patch, tmp_path/'wrong-base', base=evidence(after), patch=patch_claim,
                    result=evidence(after), policy=codec, budget=budget())
    private(patch, b'invalid patch')
    with pytest.raises(DeltaCodecError):
        apply_patch(base, patch, tmp_path/'corrupt', base=evidence(before), patch=evidence(b'invalid patch'),
                    result=evidence(after), policy=codec, budget=budget())
    assert not (tmp_path/'corrupt').exists() and not list(tmp_path.glob('.ls-openpgp-*'))


def test_uneconomic_patch_and_codec_replacement_are_not_published(tmp_path, codec):
    base, result = private(tmp_path/'base', b'x'), private(tmp_path/'new', b'y')
    assert create_patch(base, result, tmp_path/'patch', base=evidence(b'x'), result=evidence(b'y'),
                        policy=codec, budget=budget()) is None
    assert not (tmp_path/'patch').exists()
    with pytest.raises(DeltaCodecError, match='codec_changed'):
        create_patch(base, result, tmp_path/'changed', base=evidence(b'x'), result=evidence(b'y'),
                     policy=replace(codec, sha256='0'*64), budget=budget())
    assert not list(tmp_path.glob('.ls-openpgp-*'))


@pytest.mark.parametrize('behavior,code', [
    ('while :; do :; done', 'deadline_exceeded'),
    ('while :; do printf "diagnostic noise" >&2; done', 'diagnostics_limit'),
    ('while :; do printf "oversized output"; done', 'codec_failed'),
])
def test_faulty_codec_is_bounded_reaped_and_leaves_no_output(tmp_path, behavior, code):
    binary = private(tmp_path/'codec', ('#!/bin/sh\n'+behavior+'\n').encode())
    binary.chmod(0o700)
    policy = DeltaCodec(binary, hashlib.sha256(binary.read_bytes()).hexdigest(),
                        timeout_seconds=.5, max_diagnostics_bytes=100, max_output_bytes=100)
    base, new = private(tmp_path/'base', b'a'*1000), private(tmp_path/'new', b'b'*1000)
    with pytest.raises(DeltaCodecError, match=code):
        create_patch(base, new, tmp_path/'output', base=evidence(b'a'*1000), result=evidence(b'b'*1000),
                     policy=policy, budget=budget())
    assert not (tmp_path/'output').exists() and not list(tmp_path.glob('.ls-openpgp-*'))
