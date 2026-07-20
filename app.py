#!/usr/bin/env python3
"""
Launcher for Tag Validator Pro on Hugging Face Spaces (Gradio SDK).

Downloads Node.js 20 at runtime, installs npm deps + Playwright Chromium,
then starts the existing Express server on port 7860.
"""

import os
import subprocess
import signal
import sys
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))

NODE_VERSION = "v20.18.1"
NODE_DIRNAME = f"node-{NODE_VERSION}-linux-x64"
NODE_DIR = os.path.join(ROOT, NODE_DIRNAME)
NODE_BIN_DIR = os.path.join(NODE_DIR, "bin")
PLAYWRIGHT_PATH = os.path.join(ROOT, "ms-playwright")


def setup_nodejs():
    """Download and extract Node.js binary if not already present."""
    node_exe = os.path.join(NODE_BIN_DIR, "node")
    if os.path.exists(node_exe):
        print(f"[Launcher] Node.js {NODE_VERSION} already present")
        os.environ["PATH"] = NODE_BIN_DIR + ":" + os.environ.get("PATH", "")
        return

    url = f"https://nodejs.org/dist/{NODE_VERSION}/{NODE_DIRNAME}.tar.xz"
    archive = os.path.join(ROOT, "node.tar.xz")

    print(f"[Launcher] Downloading Node.js {NODE_VERSION} ...")
    subprocess.run(["curl", "-fsSL", "-o", archive, url], check=True)

    print("[Launcher] Extracting Node.js ...")
    subprocess.run(["tar", "-xf", archive, "-C", ROOT], check=True)
    os.remove(archive)

    os.environ["PATH"] = NODE_BIN_DIR + ":" + os.environ.get("PATH", "")
    ver = subprocess.run(["node", "--version"], capture_output=True, text=True)
    print(f"[Launcher] Node.js ready: {ver.stdout.strip()}")


def setup_npm_deps():
    """Run npm install for Express and friends."""
    lock = os.path.join(ROOT, "node_modules", ".package-lock.json")
    if os.path.exists(lock):
        print("[Launcher] node_modules already present, skipping npm install")
        return
    print("[Launcher] Installing npm dependencies ...")
    subprocess.run(["npm", "install", "--production"], cwd=ROOT, check=True)


def setup_playwright():
    """Install the Playwright Chromium browser (user-level, no root)."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_PATH
    if os.path.isdir(PLAYWRIGHT_PATH) and os.listdir(PLAYWRIGHT_PATH):
        print("[Launcher] Playwright browsers already installed")
        return
    print("[Launcher] Installing Playwright Chromium ...")
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        cwd=ROOT,
        check=True,
    )


def main():
    print("=" * 60)
    print("  Tag Validator Pro — HF Space Launcher")
    print("=" * 60)

    setup_nodejs()
    setup_npm_deps()
    setup_playwright()

    port = os.environ.get("PORT", "7860")
    os.environ["PORT"] = port

    print(f"\n[Launcher] Starting Express server on port {port} ...")
    proc = subprocess.Popen(["node", "server.js"], cwd=ROOT, env=os.environ)

    # Forward termination signals so the Express server shuts down cleanly
    def _forward(sig, _frame):
        proc.send_signal(sig)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    proc.wait()
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
