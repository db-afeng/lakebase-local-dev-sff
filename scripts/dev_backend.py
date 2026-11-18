"""Own the backend listener so Uvicorn can share it with Windows reload workers."""

import json
from pathlib import Path
import sys

import uvicorn
from uvicorn.supervisors import ChangeReload


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    config = uvicorn.Config(
        "app:app", host="127.0.0.1", port=int(sys.argv[1]), reload=True,
        reload_dirs=[str(root)],
        reload_excludes=["frontend/**", "notebooks/**", "tests/**", "scripts/**"],
    )
    sock = config.bind_socket()
    print("DATACART_READY " + json.dumps({"url": f"http://127.0.0.1:{sock.getsockname()[1]}"}), flush=True)
    try:
        ChangeReload(config, target=uvicorn.Server(config).run, sockets=[sock]).run()
    finally:
        sock.close()


if __name__ == "__main__":
    main()
