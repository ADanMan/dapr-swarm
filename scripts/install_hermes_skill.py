"""Install the Swarm skill from a release tag, never from uncommitted files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def install(tag, destination):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(ROOT), *args])
    if not tag.startswith('v') or any(c not in 'v0123456789.' for c in tag):
        raise ValueError('Expected a version tag such as v0.4.1')
    ref = 'refs/tags/' + tag
    commit = git('rev-parse', '--verify', ref + '^{commit}').decode().strip()
    blobs = {name: git('show', ref + ':skills/swarm/' + name)
             for name in ('SKILL.md', 'scripts/swarm.py')}
    if f'release_tag: "{tag}"'.encode() not in blobs['SKILL.md']:
        raise ValueError('Skill metadata does not match the requested release')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f'Existing skill was not overwritten: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.swarm-install-', dir=destination.parent))
    try:
        for name, content in blobs.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest = {'tag': tag, 'commit': commit,
                    'sha256': {name: hashlib.sha256(content).hexdigest() for name, content in blobs.items()}}
        (temporary / 'RELEASE.json').write_text(json.dumps(manifest, indent=2) + '\n')
        os.rename(temporary, destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='v0.4.1')
    parser.add_argument('--destination', type=Path, default=Path.home()/'.hermes/skills/swarm')
    args = parser.parse_args()
    print(json.dumps(install(args.tag, args.destination), indent=2))
