"""Identity of the running source, including uncommitted modifications."""
import hashlib
import os
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def source_identity():
    package = Path(__file__).parent
    digest = hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in sorted(package.glob("*.py")))).hexdigest()
    revision = os.environ.get("STRENGTH_TRACKER_REVISION")
    if not revision:
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=package.parent,
                                    capture_output=True, text=True, timeout=2, check=True)
            revision = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            revision = None
    return {"source_hash": digest, "code_revision": revision}
