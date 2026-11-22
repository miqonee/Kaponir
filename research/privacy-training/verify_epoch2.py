"""Verify the published paired benchmark evidence without starting a model."""
import hashlib
import json
from pathlib import Path
import tarfile


REPORTS = Path(__file__).resolve().parent / "reports" / "epoch2"


def verify_archive(suite):
    manifest = json.loads((REPORTS / f"{suite}-manifest.json").read_bytes())
    path = REPORTS / f"{suite}-evidence.tar.gz"
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["archive_sha256"]:
            raise ValueError(f"Archive checksum mismatch: {path.name}")
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        if sorted(names) != sorted(manifest["files"]):
            raise ValueError(f"Unexpected archive inventory: {path.name}")
        for name, expected in manifest["files"].items():
            with archive.extractfile(name) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError(f"Evidence checksum mismatch: {name}")
        with archive.extractfile(f"paired-{suite}.json") as stream:
            if json.load(stream) != json.loads((REPORTS / f"{suite}.json").read_bytes()):
                raise ValueError(f"Published {suite}.json differs from the archived result")
    print(f"{suite}: {len(names)} files verified; published metrics match the archive")


if __name__ == "__main__":
    for suite in ("search", "abcd"):
        verify_archive(suite)
