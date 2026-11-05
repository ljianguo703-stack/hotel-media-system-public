"""Build only our DEX helper. No manufacturer binaries or device data are bundled."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jdk", required=True, type=Path)
    parser.add_argument("--sdk", required=True, type=Path)
    parser.add_argument("--build-tools", default="36.0.0")
    parser.add_argument("--platform", default="android-34")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    suffix = ".exe" if os.name == "nt" else ""
    javac = args.jdk / "bin" / ("javac" + suffix)
    java = args.jdk / "bin" / ("java" + suffix)
    d8 = args.sdk / "build-tools" / args.build_tools / "lib" / "d8.jar"
    android = args.sdk / "platforms" / args.platform / "android.jar"
    for dependency in (javac, java, d8, android):
        if not dependency.is_file():
            parser.error(f"Missing build dependency: {dependency}")
    output = args.output.resolve()
    if output.suffix != ".jar":
        parser.error("Output must be a .jar file")
    output.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("PowerModeTool.java")
    with tempfile.TemporaryDirectory(prefix="tv-power-build-") as folder:
        subprocess.run([str(javac), "--release", "8", "-d", folder, str(source)], check=True)
        subprocess.run([
            str(java), "-cp", str(d8), "com.android.tools.r8.D8", "--min-api", "28",
            "--lib", str(android), "--output", str(output),
            str(Path(folder) / "PowerModeTool.class"),
        ], check=True)
    print(output)


if __name__ == "__main__":
    main()
