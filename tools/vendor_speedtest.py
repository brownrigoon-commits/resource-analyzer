"""Vendor a pinned, integrity-checked official Cloudflare speedtest release."""
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request

VERSION = '1.14.2'
URL = f'https://registry.npmjs.org/@cloudflare/speedtest/-/speedtest-{VERSION}.tgz'
SHA256 = '26ab2f876a525aed2ddb12dbdde98edb91aa68b23b119492798f202fe87e20f3'
ROOT = Path(__file__).resolve().parents[1]


def main():
    with urllib.request.urlopen(URL, timeout=20) as response:
        data = response.read(2_000_000)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise RuntimeError('Cloudflare speedtest archive checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as package:
        for name, destination in [
            ('package/dist/speedtest.js', ROOT / 'static/vendor' / f'cloudflare-speedtest-{VERSION}.mjs'),
            ('package/LICENSE', ROOT / 'third_party/Cloudflare-Speedtest-LICENSE.txt'),
        ]:
            payload = package.extractfile(name).read()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
    print(f'Vendored @cloudflare/speedtest {VERSION}; SHA256 {SHA256}')


if __name__ == '__main__':
    main()
