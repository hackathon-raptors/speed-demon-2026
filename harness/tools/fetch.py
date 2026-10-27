"""fetch <url> <sha256> <file>: download, and refuse the file unless its sha256 matches.

Used only while building the tools image (see Dockerfile in this directory).
"""
import hashlib
import sys
import urllib.request


def main(url, expected, dest):
    with urllib.request.urlopen(url, timeout=300) as response:
        data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        sys.exit(f"{url}: sha256 is {actual}, expected {expected}")
    with open(dest, "wb") as f:
        f.write(data)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: fetch <url> <sha256> <file>")
    main(*sys.argv[1:])
