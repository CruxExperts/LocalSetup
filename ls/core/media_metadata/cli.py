"""CLI for explicit, verified media metadata copies."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .png import prepare_png, write_copy


def main(argv=None, *, source_root=None, home=None) -> int:
    parser = argparse.ArgumentParser(prog="localsetup media")
    sub = parser.add_subparsers(dest="action", required=True)
    clean = sub.add_parser("clean-images", help="write verified PNG copies with only approved rights metadata")
    clean.add_argument("--input", type=Path, nargs="+", required=True)
    clean.add_argument("--output-directory", type=Path, required=True)
    clean.add_argument("--copyright", required=True)
    clean.add_argument("--creator", required=True)
    clean.add_argument("--website", required=True)
    clean.add_argument("--rights", required=True)
    clean.add_argument("--replace-copyright", action="store_true",
                       help="explicitly replace reviewed conflicting attribution")
    args = parser.parse_args(argv)
    metadata = {"Copyright": args.copyright, "Author": args.creator,
                "Website": args.website, "Usage Terms": args.rights}
    results = []
    try:
        sources = [path.expanduser().absolute() for path in args.input]
        output = args.output_directory.expanduser().absolute()
        destinations = [output / source.name for source in sources]
        if len(set(destinations)) != len(destinations):
            raise ValueError("Duplicate output names; choose distinct input names")
        if any(path.exists() or path.is_symlink() for path in destinations):
            raise ValueError("An output already exists; originals and prior copies are preserved")
        # Process one image at a time; retained output never scales with batch size.
        for source, target in zip(sources, destinations):
            data = prepare_png(source, metadata, replace_copyright=args.replace_copyright)
            write_copy(target, data)
            results.append({"input": str(source), "output": str(target),
                            "sha256": hashlib.sha256(data).hexdigest(), "state": "verified"})
    except (OSError, ValueError, ImportError) as exc:
        reason = ("PNG cleanup requires the optional Pillow tool environment" if isinstance(exc, ImportError)
                  else str(exc))
        print(json.dumps({"ok": False, "state": "partial" if results else "failed",
                          "reason": reason, "outputs": results}))
        return 1
    print(json.dumps({"ok": True, "state": "verified", "metadata_fields": sorted(metadata),
                      "outputs": results}))
    return 0
