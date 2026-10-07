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

# NVIDIA ships DLSS 5 / neural rendering for RTX 50 first; on older lines the stock
# nvngx_dlssnr.dll refuses to create the NR feature (Turing: error 0xbad00001).
# Lines listed here get the community build from RankFTW/rhi-repo named for that
# GPU line; every other card keeps the stock DLL. Only the neural DLL is swapped;
# every other runtime file stays stock.
NEURAL_BUILDS = {
    "20": ("dlssnr-310.8.SF-v2",
           "6eb209e764f39872625debd6abaf45e2bb6322f6f270f781f70c059ae30b3927"),
    "40": ("dlssnr-310.8.0-RTX40",
           "4b8d19bc3eff58a084f5eca7489c921501c203450169fb82ff4f649a4482ba05"),
}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gpu_series():
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        nvidia_smi = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
    if not os.path.isfile(nvidia_smi):
        return None
    try:
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except Exception:
        return None
    match = re.search(r"\bRTX\s+(20|30|40|50)\d{2}\b", result.stdout or "", re.IGNORECASE)
    return match.group(1) if match else None


def swap_neural_dll(tag, expected_sha256):
    neural_dll = runtime_dir / "nvngx_dlssnr.dll"
    if neural_dll.exists() and sha256_file(neural_dll) == expected_sha256:
        print(f"[sd-forge-dlss5] {tag} neural runtime already in place.")
        return
    zip_path = runtime_dir / "dlssnr-neural.zip"
    url = (f"https://github.com/RankFTW/rhi-repo/releases/download/{tag}/"
           f"nvngx_dlssnr_{tag.removeprefix('dlssnr-')}.zip")
    try:
        print(f"[sd-forge-dlss5] Fetching the {tag} neural runtime for this GPU (~110 MB)...")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as out_f:
            shutil.copyfileobj(resp, out_f)
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            members = [m for m in zf.namelist() if os.path.basename(m).lower() == "nvngx_dlssnr.dll"]
            if len(members) != 1:
                raise RuntimeError(f"unexpected {tag} archive layout")
            with zf.open(members[0]) as src, open(f"{neural_dll}.tmp", "wb") as dst:
                shutil.copyfileobj(src, dst)
        if sha256_file(f"{neural_dll}.tmp") != expected_sha256:
            raise RuntimeError("downloaded DLL hash mismatch")
        os.replace(f"{neural_dll}.tmp", neural_dll)
        print(f"[sd-forge-dlss5] {tag} neural runtime installed.")
    except Exception as e:
        print(f"[sd-forge-dlss5] Warning: could not install the {tag} neural runtime ({e}).")
        print("[sd-forge-dlss5] DLSS NR may not run on this GPU without it; fix and restart to retry.")
    finally:
        if zip_path.exists():
            try:
                zip_path.unlink()
            except Exception:
                pass


series = gpu_series()
if series in NEURAL_BUILDS:
    tag, expected_sha256 = NEURAL_BUILDS[series]
    swap_neural_dll(tag, expected_sha256)
