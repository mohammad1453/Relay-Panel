"""Create an application-only release archive; prints a digest, never packages secrets.

Output: dist/relay-panel-v<VERSION>.tar.gz and a matching .sha256 file.
If a wheels/ folder exists (the release workflow fills it with `pip download`),
its pure-Python wheels are included so updates can install them without internet.
The GitHub release workflow runs this same script, so local and published
archives have the same layout.
"""
import hashlib
import tarfile
from pathlib import Path

root=Path(__file__).resolve().parent.parent
version=(root/'VERSION').read_text(encoding='ascii').strip()
dist=root/'dist'
dist.mkdir(exist_ok=True)
output=dist/f'relay-panel-v{version}.tar.gz'
names=['reseller','deploy','docs','tools','tests','VERSION','CHANGELOG.md','requirements.txt','requirements.lock','requirements-dev.txt',
       'README.md','.env.example','install.sh','install-online.sh','update.sh','backup.sh','restore.sh','status.sh','uninstall.sh']
wheels=sorted((root/'wheels').glob('*-none-any.whl')) if (root/'wheels').is_dir() else []
with tarfile.open(output,'w:gz') as archive:
    for name in names:
        entry=root/name
        for path in sorted(entry.rglob('*')) if entry.is_dir() else [entry]:
            if not path.is_file() or path.is_symlink() or '__pycache__' in path.parts or path.suffix=='.pyc':
                continue
            archive.add(path,arcname=str(path.relative_to(root)).replace('\\','/'),recursive=False)
    for path in wheels:
        archive.add(path,arcname='wheels/'+path.name,recursive=False)
checksum=hashlib.sha256(output.read_bytes()).hexdigest()
output.with_name(output.name+'.sha256').write_text(checksum+'  '+output.name+'\n',encoding='ascii')
print(output,f'({len(wheels)} bundled wheels)')
print('SHA256:',checksum)
