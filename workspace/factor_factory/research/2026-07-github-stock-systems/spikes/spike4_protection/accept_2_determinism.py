"""验收2: 同一历史台账两次影子重放输出逐字节一致."""
import filecmp
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def main():
    a, b = os.path.join(HERE, "out", "det_a"), os.path.join(HERE, "out", "det_b")
    for d in (a, b):
        if os.path.isdir(d):
            shutil.rmtree(d)
    for d in (a, b):
        subprocess.run([PY, os.path.join(HERE, "shadow_replay.py"), "--out", d],
                       check=True, cwd=HERE, capture_output=True)
    names_a = sorted(os.listdir(a))
    names_b = sorted(os.listdir(b))
    assert names_a == names_b, f"file sets differ: {names_a} vs {names_b}"
    all_ok = True
    for name in names_a:
        pa, pb = os.path.join(a, name), os.path.join(b, name)
        ha, hb = sha256_file(pa), sha256_file(pb)
        same = ha == hb and filecmp.cmp(pa, pb, shallow=False)
        print(f"{name}: {'IDENTICAL' if same else 'DIFF'}  sha256={ha[:16]}...")
        all_ok = all_ok and same
    print("ACCEPT-2:", "PASS (两次重放逐字节一致)" if all_ok else "FAIL")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
