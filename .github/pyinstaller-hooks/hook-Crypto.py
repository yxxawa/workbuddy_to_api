from pathlib import Path
from PyInstaller.utils.hooks import get_package_paths
KEEP = {'_raw_aes', '_raw_aesni', '_raw_ctr', '_raw_ecb', '_ghash_clmul',
        '_ghash_portable', '_BLAKE2s', '_cpuid_c', '_strxor'}
_, package = get_package_paths('Crypto')
root = Path(package)
binaries = [(str(source), str(Path('Crypto') / source.parent.relative_to(root)))
            for source in root.rglob('*')
            if source.is_file() and source.suffix in ('.pyd', '.dll', '.so')
            and source.name.split('.')[0] in KEEP]
