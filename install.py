import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import launch

# Ensure dependencies
if not launch.is_installed("cv2"):
    launch.run_pip("install opencv-python-headless", "opencv-python for DLSS 5 optical flow")

if not launch.is_installed("av"):
    launch.run_pip("install av", "PyAV for DLSS 5 video demuxing")

# Check runtime binaries
runtime_dir = Path(__file__).resolve().parent / "bin" / "runtime"
runtime_dir.mkdir(parents=True, exist_ok=True)

required_dlls = [
    "nvngx.dll",
    "dxgi.dll",
    "renodx-dlss5.addon64",
    "nvngx_dlssnr.dll",
    "nvngx_dlss.dll",
]

# Permanent GitHub Release download link (Streamline 2.13 / DLSS 3.10 runtime package)
PRIMARY_RUNTIME_URL = "https://github.com/zeydsama/sd-forge-dlss5/releases/download/v1.0.0/DLSS310.8.0-Streamline2.13.zip"
FALLBACK_RUNTIME_URL = "https://github.com/Merserk/dlss5-visual-enhancer/releases/download/3.0/DLSS.5.Visual.Enhancer.v3.0.zip"

missing = [f for f in required_dlls if not (runtime_dir / f).exists()]
if missing:
    print(f"[sd-forge-dlss5] Missing runtime binaries: {missing}")
    print("[sd-forge-dlss5] Auto-downloading official DLSS / Streamline runtime binaries...")

    downloaded = False
    for url_label, url in [("primary", PRIMARY_RUNTIME_URL), ("fallback", FALLBACK_RUNTIME_URL)]:
        zip_path = runtime_dir / "dlss5-runtime.zip"
        try:
            print(f"[sd-forge-dlss5] Fetching package from {url_label} source...")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as out_f:
                shutil.copyfileobj(resp, out_f)

            print("[sd-forge-dlss5] Extracting runtime binaries...")
            with zipfile.ZipFile(str(zip_path), "r") as zf:
                for member in zf.namelist():
                    filename = os.path.basename(member)
                    # Extract any DLL or required binary into runtime directory
                    if filename.endswith(".dll") or filename in required_dlls:
                        with zf.open(member) as src, open(runtime_dir / filename, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        print(f"[sd-forge-dlss5]   + Extracted: {filename}")

            if zip_path.exists():
                zip_path.unlink()

            # Verify if missing files are now satisfied
            remaining = [f for f in required_dlls if not (runtime_dir / f).exists()]
            if not remaining:
                print("[sd-forge-dlss5] All DLSS 5 runtime binaries successfully installed.")
                downloaded = True
                break
            else:
                print(f"[sd-forge-dlss5] Package extracted, still awaiting: {remaining}")
        except Exception as e:
            print(f"[sd-forge-dlss5] Download failed from {url_label} source: {e}")
            if zip_path.exists():
                try:
                    zip_path.unlink()
                except Exception:
                    pass

    if not downloaded:
        still_missing = [f for f in required_dlls if not (runtime_dir / f).exists()]
        if still_missing:
            print(f"[sd-forge-dlss5] Warning: Runtime binaries incomplete ({still_missing}).")
            print("[sd-forge-dlss5] Please place nvngx.dll, dxgi.dll, renodx-dlss5.addon64, nvngx_dlssnr.dll into bin/runtime/")

# RTX 20-series (Turing): the stock nvngx_dlssnr.dll refuses to create the DLSS NR
# feature on Turing (error 0xbad00001). The community SF-v2 build of the same SDK
# version runs it. Only the neural DLL is swapped; every other file stays stock.
SFV2_NEURAL_URL = "https://github.com/RankFTW/rhi-repo/releases/download/dlssnr-310.8.SF-v2/nvngx_dlssnr_310.8.SF-v2.zip"
SFV2_NEURAL_SHA256 = "6eb209e764f39872625debd6abaf45e2bb6322f6f270f781f70c059ae30b3927"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_turing():
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        nvidia_smi = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
    if not os.path.isfile(nvidia_smi):
        return False
    try:
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except Exception:
        return False
    return bool(re.search(r"\bRTX\s+20\d{2}\b", result.stdout or "", re.IGNORECASE))


def swap_in_sfv2_neural_dll():
    neural_dll = runtime_dir / "nvngx_dlssnr.dll"
    if neural_dll.exists() and sha256_file(neural_dll) == SFV2_NEURAL_SHA256:
        print("[sd-forge-dlss5] SF-v2 neural runtime already in place.")
        return
    zip_path = runtime_dir / "sf-v2-neural.zip"
    try:
        print("[sd-forge-dlss5] Turing (RTX 20-series) detected: fetching SF-v2 neural runtime (~117 MB)...")
        req = urllib.request.Request(SFV2_NEURAL_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as out_f:
            shutil.copyfileobj(resp, out_f)
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            members = [m for m in zf.namelist() if os.path.basename(m).lower() == "nvngx_dlssnr.dll"]
            if len(members) != 1:
                raise RuntimeError("unexpected SF-v2 archive layout")
            with zf.open(members[0]) as src, open(f"{neural_dll}.tmp", "wb") as dst:
                shutil.copyfileobj(src, dst)
        if sha256_file(f"{neural_dll}.tmp") != SFV2_NEURAL_SHA256:
            raise RuntimeError("downloaded DLL hash mismatch")
        os.replace(f"{neural_dll}.tmp", neural_dll)
        print("[sd-forge-dlss5] SF-v2 neural runtime installed for Turing.")
    except Exception as e:
        print(f"[sd-forge-dlss5] Warning: could not install SF-v2 neural runtime ({e}).")
        print("[sd-forge-dlss5] DLSS NR will not run on Turing without it; fix and restart to retry.")
    finally:
        if zip_path.exists():
            try:
                zip_path.unlink()
            except Exception:
                pass


if is_turing():
    swap_in_sfv2_neural_dll()
