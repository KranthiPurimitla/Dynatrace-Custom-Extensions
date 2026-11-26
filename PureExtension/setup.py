from pathlib import Path

from setuptools import find_packages, setup


def find_version() -> str:
    version = "0.0.1"
    extension_yaml_path = Path(__file__).parent / "extension" / "extension.yaml"
    try:
        with open(extension_yaml_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("version"):
                    version = line.split(" ")[-1].strip('"')
                    break
    except Exception:
        pass
    return version


setup(
    name="custom_pure",
    version=find_version(),
    description="Custom_pure python EF2 extension",
    author="Dynatrace",
    packages=find_packages(),
    python_requires=">=3.10",
    include_package_data=True,
    install_requires=[
        "dt-extensions-sdk",
        "PyJWT[crypto]",
        "requests",
        "truststore",
        # cryptography (pulled in via PyJWT[crypto]) requires this on Python <3.11,
        # but that marker is evaluated against the *build* interpreter, not the
        # extension's target runtime (3.10). Pin it unconditionally so it's always
        # bundled into extension/lib regardless of which Python builds the package.
        "typing-extensions>=4.13.2",
    ],
    extras_require={"dev": ["dt-extensions-sdk[cli]"]},
)